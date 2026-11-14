"""All SQL for the reviewer's automation summary (ADR-021): counts over recent runs.

Counts only. The definitions live here, next to the SQL, so they cannot drift apart:

- runs: workflow runs started since `since`;
- resolved automatically: those runs that completed with no refund proposal, so no person
  was involved (the status was explained or the question answered);
- sent for approval: those runs with a refund proposal (each waits for a reviewer);
- escalated: those runs that ended escalated;
- time to outcome: from the run's start to its first outcome event (completed, escalated,
  failed, or approval requested). It measures the system, not how long a reviewer took.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import ActionProposal, AuditEvent, WorkflowRun
from support_agent.domain.enums import RunStatus

OUTCOME_EVENT_TYPES = ("run.completed", "run.escalated", "run.failed", "approval.requested")


@dataclass(frozen=True)
class AutomationCounts:
    runs: int
    resolved_automatically: int
    sent_for_approval: int
    escalated: int
    median_seconds_to_outcome: float | None


async def counts_since(session: AsyncSession, since: datetime) -> AutomationCounts:
    has_proposal = exists().where(ActionProposal.run_id == WorkflowRun.id)
    recent = WorkflowRun.started_at >= since
    totals = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(and_(WorkflowRun.status == RunStatus.COMPLETED, ~has_proposal)),
                func.count().filter(has_proposal),
                func.count().filter(WorkflowRun.status == RunStatus.ESCALATED),
            ).where(recent)
        )
    ).one()

    # Each recent run's first outcome time, then the median of (outcome - start) in seconds.
    first_outcome = (
        select(
            WorkflowRun.started_at.label("started_at"),
            func.min(AuditEvent.created_at).label("outcome_at"),
        )
        .join(AuditEvent, AuditEvent.run_id == WorkflowRun.id)
        .where(recent, AuditEvent.event_type.in_(OUTCOME_EVENT_TYPES))
        .group_by(WorkflowRun.id, WorkflowRun.started_at)
        .subquery()
    )
    seconds = func.extract("epoch", first_outcome.c.outcome_at - first_outcome.c.started_at)
    median = await session.scalar(
        select(func.percentile_cont(0.5).within_group(seconds)).select_from(first_outcome)
    )
    return AutomationCounts(
        runs=totals[0],
        resolved_automatically=totals[1],
        sent_for_approval=totals[2],
        escalated=totals[3],
        median_seconds_to_outcome=None if median is None else float(median),
    )
