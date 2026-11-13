"""The Postgres job queue (worker/queue.py) against a real database.

Claims across workers need separate connections that see each other's commits, so those
tests use `committed_ticket` (real commits, cleaned up afterwards). The lease and fencing
tests need only one connection and use the usual rolled-back session.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.db.models import Customer, Job, Ticket, WorkflowRun
from support_agent.domain.enums import JobStatus, JobType, RunMode, RunStatus
from support_agent.repositories import workflow_runs
from support_agent.worker import queue

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
LEASE = 60


Sessions = async_sessionmaker[AsyncSession]


async def _committed_jobs(sessions: Sessions, ticket_id: UUID, count: int) -> set[UUID]:
    """`count` queued start_run jobs, each on its own (finished) run of the committed ticket."""
    ids = set()
    async with sessions() as db:
        for _ in range(count):
            run = await workflow_runs.create_agent_run(
                db, ticket_id=ticket_id, mode=RunMode.MULTI_AGENT, now=NOW
            )
            run.status = RunStatus.COMPLETED  # finished, so the next run is allowed
            await db.flush()
            job = await queue.enqueue(
                db, JobType.START_RUN, run_id=run.id, payload={}, now=NOW, max_attempts=3
            )
            ids.add(job.id)
        await db.commit()
    return ids


async def test_two_workers_never_claim_the_same_job(
    committed_sessions: Sessions, committed_ticket: tuple[UUID, UUID]
) -> None:
    expected = await _committed_jobs(committed_sessions, committed_ticket[1], 10)

    async def worker() -> list[UUID]:
        claimed = []
        async with committed_sessions() as db:
            while (job := await queue.claim(db, now=NOW, lease_seconds=LEASE)) is not None:
                claimed.append(job.id)
                await asyncio.sleep(0)  # let the other worker in between claims
        return claimed

    first, second = await asyncio.gather(worker(), worker())

    assert not set(first) & set(second)  # nobody got a job twice
    assert set(first) | set(second) == expected  # and every job was claimed once
    assert len(first) + len(second) == len(expected)


async def test_a_locked_job_is_skipped_not_waited_for(
    committed_sessions: Sessions, committed_ticket: tuple[UUID, UUID]
) -> None:
    ids = await _committed_jobs(committed_sessions, committed_ticket[1], 2)
    async with committed_sessions() as holder, committed_sessions() as other:
        # Worker A is in the middle of claiming the oldest job: its row is locked.
        locked = await holder.scalar(
            select(Job)
            .where(Job.id.in_(ids))
            .order_by(Job.available_at, Job.id)
            .limit(1)
            .with_for_update()
        )
        assert locked is not None

        # Worker B does not wait for that lock: it takes the other job at once.
        claimed = await asyncio.wait_for(
            queue.claim(other, now=NOW, lease_seconds=LEASE), timeout=5
        )

        assert claimed is not None
        assert claimed.id in ids - {locked.id}
        await holder.rollback()


# ---------------------------------------------------------------- leases and fencing


async def _one_job(session: AsyncSession, max_attempts: int = 3) -> UUID:
    customer = Customer(external_auth_subject="queue-test", name="Q", email="queue@example.com")
    session.add(customer)
    await session.flush()
    ticket = Ticket(customer_id=customer.id, summary="queue test")
    session.add(ticket)
    await session.flush()
    run = WorkflowRun(ticket_id=ticket.id, thread_id="run-queue-test", started_at=NOW)
    session.add(run)
    await session.flush()
    job = await queue.enqueue(
        session, JobType.START_RUN, run_id=run.id, payload={}, now=NOW, max_attempts=max_attempts
    )
    await session.commit()
    return job.id


async def _job(session: AsyncSession, job_id: UUID) -> Job:
    found = await session.get(Job, job_id, populate_existing=True)
    assert found is not None
    return found


async def test_a_job_is_reclaimed_only_after_its_lease_runs_out(session: AsyncSession) -> None:
    job_id = await _one_job(session)

    first = await queue.claim(session, now=NOW, lease_seconds=LEASE)
    assert first is not None and (first.id, first.attempt) == (job_id, 1)

    # While the lease holds, nobody else can take it...
    assert (
        await queue.claim(session, now=NOW + timedelta(seconds=LEASE - 1), lease_seconds=LEASE)
        is None
    )
    # ...and a heartbeat pushes the lease forward.
    assert await queue.heartbeat(
        session, first, now=NOW + timedelta(seconds=30), lease_seconds=LEASE
    )
    assert (
        await queue.claim(session, now=NOW + timedelta(seconds=LEASE + 1), lease_seconds=LEASE)
        is None
    )

    # The worker dies: no more heartbeats. Once the lease has run out, another worker takes it.
    later = NOW + timedelta(seconds=30 + LEASE + 1)
    second = await queue.claim(session, now=later, lease_seconds=LEASE)
    assert second is not None and (second.id, second.attempt) == (job_id, 2)


async def test_a_worker_that_lost_its_job_cannot_finish_it(session: AsyncSession) -> None:
    """The attempt number is a fencing token: only the current holder's writes land."""
    job_id = await _one_job(session)
    stale = await queue.claim(session, now=NOW, lease_seconds=LEASE)
    assert stale is not None
    current = await queue.claim(
        session, now=NOW + timedelta(seconds=LEASE + 1), lease_seconds=LEASE
    )
    assert current is not None

    # The first worker wakes up and tries every write it could make. None of them land.
    assert not await queue.heartbeat(session, stale, now=NOW, lease_seconds=LEASE)
    assert not await queue.complete(session, stale)
    assert not await queue.retry(session, stale, now=NOW, delay_seconds=0, code="X")
    assert not await queue.mark_dead(session, stale, code="X")
    assert not await queue.release(session, stale, now=NOW)
    job = await _job(session, job_id)
    assert (job.status, job.attempts, job.last_error_code) == (JobStatus.RUNNING, 2, None)

    # The current holder's result stands.
    assert await queue.complete(session, current)
    assert (await _job(session, job_id)).status == JobStatus.SUCCEEDED


async def test_retry_waits_for_its_backoff_and_release_does_not_count(
    session: AsyncSession,
) -> None:
    job_id = await _one_job(session)
    claimed = await queue.claim(session, now=NOW, lease_seconds=LEASE)
    assert claimed is not None

    assert await queue.retry(session, claimed, now=NOW, delay_seconds=10, code="TimeoutError")

    job = await _job(session, job_id)
    assert (job.status, job.attempts, job.last_error_code) == (
        JobStatus.QUEUED,
        1,
        "TimeoutError",
    )
    assert await queue.claim(session, now=NOW + timedelta(seconds=9), lease_seconds=LEASE) is None
    again = await queue.claim(session, now=NOW + timedelta(seconds=10), lease_seconds=LEASE)
    assert again is not None and again.attempt == 2

    # A graceful shutdown gives it back at once, and the interrupted attempt is not counted.
    assert await queue.release(session, again, now=NOW + timedelta(seconds=11))
    job = await _job(session, job_id)
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 1)


async def test_only_one_expiry_sweep_is_queued_at_a_time(session: AsyncSession) -> None:
    assert await queue.schedule_expiry_sweep(session, now=NOW, max_attempts=3)
    assert not await queue.schedule_expiry_sweep(session, now=NOW, max_attempts=3)
    sweeps = await session.scalars(select(Job).where(Job.job_type == JobType.EXPIRE_APPROVALS))
    assert len(sweeps.all()) == 1
