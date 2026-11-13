"""The worker loop: claim a job, run its handler while renewing the lease, record the result.

    loop:
        every approval_expiry_check_seconds: queue an expiry sweep (if none is waiting)
        claim the next due job (or sleep for the poll interval)
        run its handler, with a heartbeat task renewing the lease
        success           -> complete
        PermanentJobError -> dead at once, run escalated
        other exception   -> retry with backoff, or dead (run escalated) on the last attempt

Every write after the claim is fenced by the attempt number (worker/queue.py), so a worker
that lost its lease cannot overwrite the result of the worker that took the job over.

Shutdown (worker/main.py sets `stop`): no new job is claimed; the current one gets
`worker_shutdown_grace_seconds` to finish, then it is cancelled and released (queued again
straight away, the attempt not counted). Its work so far is safe: the checkpointer saved
every finished step, and the next worker continues from there.

`drain` runs every due job to the end in the calling task, with no heartbeat. Tests use it
to play the worker's part after an API call.
"""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

from support_agent.core.errors import PermanentJobError
from support_agent.core.logging import run_id_var
from support_agent.worker import queue
from support_agent.worker.handlers import HANDLERS, WorkerDeps, escalate_dead_job
from support_agent.worker.queue import ClaimedJob

log = logging.getLogger(__name__)

# A safety stop for `drain`: far more jobs than any test queues.
_DRAIN_LIMIT = 100


def _fields(job: ClaimedJob) -> dict[str, object]:
    return {"job_id": str(job.id), "job_type": job.job_type.value, "attempt": job.attempt}


async def claim_next(deps: WorkerDeps) -> ClaimedJob | None:
    async with deps.sessions() as session:
        return await queue.claim(
            session, now=deps.clock(), lease_seconds=deps.settings.job_lease_seconds
        )


async def execute(job: ClaimedJob, deps: WorkerDeps, *, heartbeat: bool = True) -> None:
    """Run one claimed job and record how it went. Never raises, except on cancellation."""
    token = run_id_var.set(None if job.run_id is None else str(job.run_id))
    try:
        if job.attempt > job.max_attempts:
            # Its last attempt was claimed, then its worker died; the lease ran out since.
            await _bury(job, deps, "ATTEMPTS_EXHAUSTED")
            return
        log.info("job_started", extra=_fields(job))
        beat = asyncio.create_task(_heartbeat(job, deps)) if heartbeat else None
        try:
            async with deps.sessions() as session:
                await HANDLERS[job.job_type](session, job, deps)
        except asyncio.CancelledError:
            await asyncio.shield(_release(job, deps))
            raise
        except PermanentJobError as exc:
            await _bury(job, deps, exc.code)
        except Exception as exc:
            await _fail(job, deps, exc)
        else:
            await _complete(job, deps)
        finally:
            if beat is not None:
                beat.cancel()
                with suppress(asyncio.CancelledError):
                    await beat
    finally:
        run_id_var.reset(token)


async def _complete(job: ClaimedJob, deps: WorkerDeps) -> None:
    async with deps.sessions() as session:
        if await queue.complete(session, job):
            log.info("job_succeeded", extra=_fields(job))
        else:  # another worker took it over; its result stands
            log.warning("job_lost_lease", extra=_fields(job))


async def _fail(job: ClaimedJob, deps: WorkerDeps, exc: Exception) -> None:
    code = queue.error_code(exc)
    # The code only: an exception's message or traceback can hold customer text.
    log.warning("job_failed", extra={**_fields(job), "error_code": code})
    if job.attempt >= job.max_attempts:
        await _bury(job, deps, code)
        return
    delay = queue.backoff_delay(
        job.attempt,
        base_seconds=deps.settings.job_backoff_base_seconds,
        max_seconds=deps.settings.job_backoff_max_seconds,
    )
    async with deps.sessions() as session:
        await queue.retry(session, job, now=deps.clock(), delay_seconds=delay, code=code)


async def _bury(job: ClaimedJob, deps: WorkerDeps, code: str) -> None:
    """Mark the job dead and escalate its run, in one transaction."""
    async with deps.sessions() as session:
        if await queue.mark_dead(session, job, code=code):
            await escalate_dead_job(session, job, code, deps.clock())
        await session.commit()
    log.error("job_dead", extra={**_fields(job), "error_code": code})


async def _release(job: ClaimedJob, deps: WorkerDeps) -> None:
    async with deps.sessions() as session:
        await queue.release(session, job, now=deps.clock())
    log.info("job_released", extra=_fields(job))


async def _heartbeat(job: ClaimedJob, deps: WorkerDeps) -> None:
    """Renew the lease until cancelled. Stops if the job was taken over."""
    settings = deps.settings
    while True:
        await asyncio.sleep(settings.job_heartbeat_seconds)
        try:
            async with deps.sessions() as session:
                held = await queue.heartbeat(
                    session, job, now=deps.clock(), lease_seconds=settings.job_lease_seconds
                )
        except Exception as exc:  # e.g. a dropped connection; try again next beat
            log.warning(
                "heartbeat_failed", extra={**_fields(job), "error_code": type(exc).__name__}
            )
            continue
        if not held:
            log.warning("job_lost_lease", extra=_fields(job))
            return


async def drain(deps: WorkerDeps) -> int:
    """Run every job that is due now, one after another. Returns how many ran."""
    ran = 0
    while ran < _DRAIN_LIMIT:
        job = await claim_next(deps)
        if job is None:
            return ran
        await execute(job, deps, heartbeat=False)
        ran += 1
    raise RuntimeError("drain: too many jobs; is a job re-queuing itself?")


async def run_forever(deps: WorkerDeps, stop: asyncio.Event) -> None:
    """The worker process's main loop, until `stop` is set."""
    settings = deps.settings
    next_sweep = deps.clock()
    while not stop.is_set():
        if deps.clock() >= next_sweep:
            await _schedule_sweep(deps)
            next_sweep = deps.clock() + timedelta(seconds=settings.approval_expiry_check_seconds)
        try:
            worked = await _run_next(deps, stop)
        except Exception as exc:  # e.g. the database is down; keep the worker alive
            log.warning("worker_loop_error", extra={"error_code": type(exc).__name__})
            worked = False
        if not worked:
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.worker_poll_interval_seconds)


async def _schedule_sweep(deps: WorkerDeps) -> None:
    try:
        async with deps.sessions() as session:
            await queue.schedule_expiry_sweep(
                session, now=deps.clock(), max_attempts=deps.settings.job_max_attempts
            )
    except Exception as exc:
        log.warning("sweep_schedule_failed", extra={"error_code": type(exc).__name__})


async def _run_next(deps: WorkerDeps, stop: asyncio.Event) -> bool:
    """Claim and run one job; on shutdown give it the grace period, then release it."""
    job = await claim_next(deps)
    if job is None:
        return False
    work = asyncio.create_task(execute(job, deps))
    stopping = asyncio.create_task(stop.wait())
    await asyncio.wait({work, stopping}, return_when=asyncio.FIRST_COMPLETED)
    stopping.cancel()
    if not work.done():
        log.info("shutdown_waiting_for_job", extra=_fields(job))
        await asyncio.wait({work}, timeout=deps.settings.worker_shutdown_grace_seconds)
    if not work.done():
        work.cancel()  # execute() releases the job before it stops
    with suppress(asyncio.CancelledError):
        await work
    return True
