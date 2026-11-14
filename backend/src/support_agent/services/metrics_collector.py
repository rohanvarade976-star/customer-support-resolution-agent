"""Fill the database-derived gauges just before /metrics is served (ADR-018).

Only the API calls this. The worker never does, so summing API and worker metrics counts
each queue row and refund once.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core import metrics
from support_agent.domain.enums import RefundOperationStatus
from support_agent.repositories import metrics as metrics_repo


async def refresh_database_gauges(session: AsyncSession, now: datetime) -> None:
    stats = await metrics_repo.queue_stats(session, now)
    metrics.JOBS_QUEUED.set(stats.queued)
    metrics.JOBS_DEAD.set(stats.dead)
    oldest = stats.oldest_due_available_at
    metrics.JOBS_OLDEST_QUEUED_AGE.set(
        0 if oldest is None else max(0.0, (now - oldest).total_seconds())
    )
    metrics.RECONCILIATION_BACKLOG.set(await metrics_repo.reconciliation_backlog(session, now))
    counts = await metrics_repo.refund_counts(session)
    for (
        status
    ) in RefundOperationStatus:  # every status, so a series drops to 0 instead of going stale
        metrics.REFUNDS.labels(status=status.value).set(counts.get(status, 0))
