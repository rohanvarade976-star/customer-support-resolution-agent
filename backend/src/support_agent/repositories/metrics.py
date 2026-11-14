"""All SQL for the database-derived metrics (ADR-018). Counts and ages only."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import Job, RefundOperation
from support_agent.domain.enums import JobStatus, RefundOperationStatus


@dataclass(frozen=True)
class QueueStats:
    queued: int
    dead: int
    oldest_due_available_at: datetime | None  # the oldest queued job that is due now


async def queue_stats(session: AsyncSession, now: datetime) -> QueueStats:
    queued, dead, oldest = (
        await session.execute(
            select(
                func.count().filter(Job.status == JobStatus.QUEUED),
                func.count().filter(Job.status == JobStatus.DEAD),
                # A job delayed by retry backoff is not late, so only due jobs set the age.
                func.min(Job.available_at).filter(
                    Job.status == JobStatus.QUEUED, Job.available_at <= now
                ),
            )
        )
    ).one()
    return QueueStats(queued=queued, dead=dead, oldest_due_available_at=oldest)


async def refund_counts(session: AsyncSession) -> dict[RefundOperationStatus, int]:
    rows = await session.execute(
        select(RefundOperation.status, func.count()).group_by(RefundOperation.status)
    )
    return {status: count for status, count in rows.all()}


async def reconciliation_backlog(session: AsyncSession, now: datetime) -> int:
    """Refund operations a reconciler may check right now (this uses the partial index)."""
    count = await session.scalar(
        select(func.count()).where(
            RefundOperation.next_reconcile_at.is_not(None),
            RefundOperation.next_reconcile_at <= now,
        )
    )
    return count or 0
