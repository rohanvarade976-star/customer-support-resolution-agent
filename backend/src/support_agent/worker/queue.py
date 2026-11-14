"""The Postgres job queue: every SQL statement on the `jobs` table (ADR-012).

The life of a job:

    enqueue (in the caller's transaction) -> queued
    claim   -> running, attempts + 1, lease until now + lease
    heartbeat (every few seconds while working) pushes the lease forward
    complete -> succeeded
    retry    -> queued again, available_at = now + backoff with jitter
    mark_dead -> dead (all attempts used, or a failure that retrying cannot fix)
    release  -> queued at once, attempt not counted (graceful shutdown)

Two guarantees:

1. **Two workers never claim the same job.** `claim` uses SELECT ... FOR UPDATE SKIP LOCKED:
   a row another worker is claiming right now is skipped, not waited for. The claim is
   committed straight away, so the row lock lasts milliseconds, never the job's runtime.
2. **A worker that lost its job cannot finish it.** If a worker stalls past its lease,
   another worker may claim the job again, which adds one to `attempts`. Every later write
   (heartbeat, complete, retry, mark_dead, release) only matches
   `status = 'running' AND attempts = <the attempt this worker claimed>`. The attempt number
   is the "fencing token": the stale worker's write matches no row and changes nothing.

`enqueue` does not commit, so a run and its job are committed together or not at all. Every
other function here commits its own short transaction.

Times come from the caller's clock (the app's clock), so tests can move time forward.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.errors import AppError
from support_agent.core.tracing import inject_traceparent
from support_agent.db.models import Job
from support_agent.db.models.jobs import OPEN_SWEEP_SQL
from support_agent.domain.enums import SWEEP_JOB_TYPES, JobStatus, JobType

TRACEPARENT_KEY = "traceparent"


@dataclass(frozen=True)
class ClaimedJob:
    """A job this worker holds. `attempt` is the fencing token for every later write."""

    id: UUID
    job_type: JobType
    run_id: UUID | None
    payload: dict[str, Any]
    attempt: int
    max_attempts: int


async def enqueue(
    session: AsyncSession,
    job_type: JobType,
    *,
    run_id: UUID | None,
    payload: dict[str, Any],
    now: datetime,
    max_attempts: int,
) -> Job:
    """Add a job in the caller's transaction (flushed, not committed). Payload: ids only.

    Inside a recorded trace, the payload also gets the W3C `traceparent` (a trace id and a
    span id), so the worker's spans join the request's trace (ADR-018).
    """
    traceparent = inject_traceparent()
    if traceparent is not None:
        payload = {**payload, TRACEPARENT_KEY: traceparent}
    job = Job(
        job_type=job_type,
        run_id=run_id,
        payload=payload,
        max_attempts=max_attempts,
        available_at=now,
    )
    session.add(job)
    await session.flush()
    return job


async def schedule_sweep(
    session: AsyncSession, job_type: JobType, *, now: datetime, max_attempts: int
) -> bool:
    """Queue a scheduled sweep unless one of its type is already queued or running. Commits.

    Several workers may call this at the same moment; the partial unique index
    `uq_jobs_one_open_sweep_per_type` lets exactly one insert through and the others do
    nothing.
    """
    if job_type not in SWEEP_JOB_TYPES:
        raise ValueError(f"{job_type} is not a scheduled sweep")
    stmt = (
        insert(Job)
        .values(
            job_type=job_type,
            run_id=None,
            payload={},
            max_attempts=max_attempts,
            available_at=now,
        )
        .on_conflict_do_nothing(index_elements=[Job.job_type], index_where=text(OPEN_SWEEP_SQL))
        .returning(Job.id)
    )
    inserted = (await session.execute(stmt)).scalar_one_or_none() is not None
    await session.commit()
    return inserted


async def claim(session: AsyncSession, *, now: datetime, lease_seconds: int) -> ClaimedJob | None:
    """Take the next due job, or None. Commits, so the row lock is released at once.

    Due means: queued and its available_at has passed, or running with a lease that has run
    out (its worker died or stalled). Oldest first.
    """
    stmt = (
        select(Job)
        .where(
            or_(
                and_(Job.status == JobStatus.QUEUED, Job.available_at <= now),
                and_(Job.status == JobStatus.RUNNING, Job.lease_expires_at <= now),
            )
        )
        .order_by(Job.available_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    job = await session.scalar(stmt)
    if job is None:
        await session.commit()  # end the (empty) transaction
        return None
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    claimed = ClaimedJob(
        id=job.id,
        job_type=job.job_type,
        run_id=job.run_id,
        payload=dict(job.payload),
        attempt=job.attempts,
        max_attempts=job.max_attempts,
    )
    await session.commit()
    return claimed


async def _fenced_update(session: AsyncSession, job: ClaimedJob, **values: Any) -> bool:
    """Update the job only if this worker still holds it (same attempt, still running)."""
    stmt = (
        update(Job)
        .where(
            Job.id == job.id,
            Job.status == JobStatus.RUNNING,
            Job.attempts == job.attempt,
        )
        .values(**values)
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    )
    matched = (await session.execute(stmt)).scalar_one_or_none() is not None
    await session.commit()
    return matched


async def heartbeat(
    session: AsyncSession, job: ClaimedJob, *, now: datetime, lease_seconds: int
) -> bool:
    """Renew the lease. False: the job was taken over, so this worker should stop caring."""
    return await _fenced_update(
        session, job, lease_expires_at=now + timedelta(seconds=lease_seconds)
    )


async def complete(session: AsyncSession, job: ClaimedJob) -> bool:
    return await _fenced_update(
        session, job, status=JobStatus.SUCCEEDED, lease_expires_at=None, last_error_code=None
    )


async def retry(
    session: AsyncSession, job: ClaimedJob, *, now: datetime, delay_seconds: float, code: str
) -> bool:
    return await _fenced_update(
        session,
        job,
        status=JobStatus.QUEUED,
        available_at=now + timedelta(seconds=delay_seconds),
        lease_expires_at=None,
        last_error_code=code,
    )


async def mark_dead(session: AsyncSession, job: ClaimedJob, *, code: str) -> bool:
    """Not committed: the caller escalates the run in the same transaction, then commits."""
    stmt = (
        update(Job)
        .where(Job.id == job.id, Job.status == JobStatus.RUNNING, Job.attempts == job.attempt)
        .values(status=JobStatus.DEAD, lease_expires_at=None, last_error_code=code)
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def release(session: AsyncSession, job: ClaimedJob, *, now: datetime) -> bool:
    """Give the job back on shutdown: queued at once, and this attempt does not count."""
    return await _fenced_update(
        session,
        job,
        status=JobStatus.QUEUED,
        attempts=job.attempt - 1,
        available_at=now,
        lease_expires_at=None,
    )


async def latest_for_run(session: AsyncSession, run_id: UUID) -> Job | None:
    """The run's newest job, for GET /runs/{id}."""
    stmt = (
        select(Job)
        .where(Job.run_id == run_id)
        .order_by(Job.created_at.desc(), Job.id)
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


def backoff_delay(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
    rand: Callable[[], float] = random.random,
) -> float:
    """Seconds to wait before retry number `attempt` (1 = after the first failure).

    Exponential: the ceiling doubles each attempt, up to `max_seconds`. "Equal jitter": the
    wait is a random point in the top half of the ceiling, so retries of many jobs that
    failed together spread out, yet each still waits at least half the ceiling.
    """
    ceiling = min(max_seconds, base_seconds * 2.0 ** (attempt - 1))
    return ceiling / 2 + rand() * ceiling / 2


def error_code(exc: BaseException) -> str:
    """A code for `last_error_code`. Never the message: it can contain customer data."""
    code = exc.error_code if isinstance(exc, AppError) else type(exc).__name__
    return code[:64]
