"""The reviewer's AI analysis of a run, the review queue's AI signals and the automation
summary (ADR-021). Reviewers only: the routes check the role before anything here runs.

The analysis is computed by code from the audit trail (domain/run_analysis.py). The one
model-written piece, the accepted draft's rationale, is read from the run's checkpoint and
returned as data labelled AI-generated and unverified. Nothing here writes anything, and
nothing reads the rationale to decide an amount or an action.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.db.models import AuditEvent
from support_agent.domain.run_analysis import (
    QUEUE_EVENT_TYPES,
    EventView,
    analyze_run,
    recommendation,
    risk_flags,
)
from support_agent.repositories import audit, automation
from support_agent.schemas.auth import Principal
from support_agent.schemas.run_analysis import (
    AgentActivity,
    AiRecommendation,
    AutomationSummaryResponse,
    QualityChecks,
    RiskFlag,
    RunAnalysisResponse,
    RunCost,
    ToolUse,
)
from support_agent.services.runs import load_visible_run
from support_agent.workflow.graph import Checkpointer, accepted_rationale

SUMMARY_DAYS = 7


def _view(event: AuditEvent) -> EventView:
    return EventView(event.event_type, event.actor, event.created_at, event.redacted_payload)


async def get_analysis(
    session: AsyncSession,
    checkpointer: Checkpointer,
    settings: Settings,
    principal: Principal,
    run_id: UUID,
) -> RunAnalysisResponse:
    run, _ticket = await load_visible_run(session, principal, run_id)  # 404 if no such run
    events = [_view(event) for event in await audit.list_for_run(session, run.id)]
    result = analyze_run(events, settings.classifier_confidence_threshold)
    rec = result.recommendation
    # Only an accepted draft's rationale: a rejected draft was replaced by code, so its
    # reasoning is not what the case rests on.
    rationale = await accepted_rationale(checkpointer, run.thread_id) if rec.accepted else None
    return RunAnalysisResponse(
        run_id=run.id,
        mode=run.mode,
        status=run.status,
        recommendation=AiRecommendation(
            action=rec.action,
            accepted=rec.accepted,
            reason_codes=rec.reason_codes,
            rationale=rationale,
        ),
        agents=[AgentActivity.model_validate(row) for row in result.agents],
        tools=[ToolUse.model_validate(tool) for tool in result.tools],
        quality=QualityChecks.model_validate(result.quality),
        risk_flags=[RiskFlag.model_validate(flag) for flag in result.risk_flags],
        cost=RunCost.model_validate(result.cost),
    )


@dataclass(frozen=True)
class QueueSignals:
    """What the review queue shows per row: codes only, no model text."""

    ai_action: str | None
    risk_flags: list[str]


async def queue_signals(
    session: AsyncSession, settings: Settings, run_ids: list[UUID]
) -> dict[UUID, QueueSignals]:
    """The AI's recommended action and the risk flag codes for each run, from one query.

    Read from the audit trail only (no checkpoint reads), so the queue stays one cheap call.
    """
    events = await audit.list_of_types_for_runs(session, run_ids, QUEUE_EVENT_TYPES)
    by_run: dict[UUID, list[EventView]] = {run_id: [] for run_id in run_ids}
    for event in events:
        if event.run_id is not None:
            by_run[event.run_id].append(_view(event))
    threshold = settings.classifier_confidence_threshold
    return {
        run_id: QueueSignals(
            ai_action=recommendation(views).action,
            risk_flags=[flag.code.value for flag in risk_flags(views, threshold)],
        )
        for run_id, views in by_run.items()
    }


async def automation_summary(
    session: AsyncSession, now: datetime, days: int = SUMMARY_DAYS
) -> AutomationSummaryResponse:
    since = now - timedelta(days=days)
    counts = await automation.counts_since(session, since)
    return AutomationSummaryResponse(
        days=days,
        since=since,
        runs=counts.runs,
        resolved_automatically=counts.resolved_automatically,
        sent_for_approval=counts.sent_for_approval,
        escalated=counts.escalated,
        median_seconds_to_outcome=counts.median_seconds_to_outcome,
    )
