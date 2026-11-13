"""Save what one segment of a run did: statuses, the ticket's reply, audit events, usage.

A run can now take several calls: start -> pause -> resume -> ... -> end. Each call runs one
segment (workflow/graph.py `Segment`). Events are written from `segment.written`, which holds
only what nodes wrote in this segment, so a resumed run never repeats earlier events. The
whole state (`segment.state`) is used only for where the run now stands.

Audit payloads hold ids, labels, numbers and codes only. The classifier's `reason`, a
rejected order number, the supervisor's hints, the investigator's plan, facts and tool
results, policy text, the drafted message and the customer's replies are all left out.
Events: `run.classified`, `supervisor.decided`, `investigation.started`, one `tool.called`
per call, `investigation.completed`, `policy.completed`, `resolution.completed`,
`proposal.created` (written by services/proposals.py), `clarification.requested`,
`run.paused`, the run's end event, and `run.usage`. Refund events come from the refund
executor itself.

When the run has finished, its verified outcome is also remembered for the customer
(services/memory.py, ADR-013): fixed codes derived from statuses, never text.

Nothing is committed here; the caller commits.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agents.classifier import Classification
from support_agent.core.config import Settings
from support_agent.db.models import Ticket, WorkflowRun
from support_agent.domain.enums import RunMode, RunStatus, TicketCategory, TicketStatus
from support_agent.domain.memory import memory_outcome
from support_agent.llm.tool_loop import OUTCOME_OK, ToolCallRecord
from support_agent.llm.usage import UsageMeter
from support_agent.repositories import audit, tickets, workflow_runs
from support_agent.services import memory
from support_agent.services.case_updates import set_run_status, set_ticket_status
from support_agent.workflow.graph import Segment
from support_agent.workflow.state import AskedFor, OrderHint, WorkflowState

# Where the ticket stands once the run stops at each state. Nodes that need something else
# (after a reviewer's decision) set `ticket_status` themselves.
_TICKET_STATUS_FOR_RUN = {
    RunStatus.AWAITING_CUSTOMER: TicketStatus.AWAITING_CUSTOMER,
    RunStatus.AWAITING_APPROVAL: TicketStatus.IN_PROGRESS,
    RunStatus.COMPLETED: TicketStatus.RESOLVED,
    RunStatus.ESCALATED: TicketStatus.ESCALATED,
}
_WAITING = frozenset({RunStatus.AWAITING_CUSTOMER, RunStatus.AWAITING_APPROVAL})


async def save_segment(
    session: AsyncSession,
    settings: Settings,
    run: WorkflowRun,
    ticket: Ticket,
    segment: Segment,
    meter: UsageMeter,
    now: datetime,
) -> None:
    final, written = segment.state, segment.written
    status = final["status"]
    if (status in _WAITING) != segment.paused:  # a graph bug: never leave a run half-open
        raise RuntimeError(f"run stopped as {status.value} with paused={segment.paused}")

    classification = written.get("classification")
    if classification is not None:  # the classifier ran in this segment
        await _record_classification(session, settings, run, ticket, written, classification)
    await _record_gathering(session, run, written)
    await _record_resolution(session, run, written)

    details: dict[str, object] = {}
    if errors := written.get("errors"):
        details["error_codes"] = [e.value for e in errors]
    for key in ("proposal_refusal", "refund_refusal"):
        if key in written:
            details[key] = written[key]
    await set_run_status(session, run, status, now=now, actor="system", details=details or None)

    if segment.paused:
        await audit.record(
            session,
            run_id=run.id,
            actor="system",
            event_type="run.paused",
            payload={"run_id": str(run.id), "reason": status.value},
        )
    if status == RunStatus.AWAITING_CUSTOMER:
        await audit.record(
            session,
            run_id=run.id,
            actor="system",
            event_type="clarification.requested",
            payload={"reason": _clarification_reason(final)},
        )
    if reply := written.get("customer_reply"):
        await tickets.set_reply(session, ticket, reply)
    # KeyError = a graph bug (a state with no ticket mapping) -> the run is marked failed.
    ticket_status = written.get("ticket_status") or _TICKET_STATUS_FOR_RUN[status]
    await set_ticket_status(session, ticket, ticket_status, run_id=run.id, actor="system")
    await _record_usage(session, run, meter)
    await _remember_outcome(session, settings, run, ticket, final, now)


async def _remember_outcome(
    session: AsyncSession,
    settings: Settings,
    run: WorkflowRun,
    ticket: Ticket,
    final: WorkflowState,
    now: datetime,
) -> None:
    """Remember a finished run's verified outcome. Codes only: no text from the state."""
    outcome = final.get("outcome")
    remembered = memory_outcome(
        run_status=final["status"],
        refund_status=final.get("refund_status"),
        approval_decision=final.get("approval_decision"),
        action=None if outcome is None else outcome.action,
    )
    if remembered is None:
        return
    kind, code = remembered
    await memory.record_run_outcome(
        session,
        settings,
        run=run,
        ticket=ticket,
        kind=kind,
        outcome=code,
        order_number=final.get("order_number"),
        now=now,
    )


async def _record_classification(
    session: AsyncSession,
    settings: Settings,
    run: WorkflowRun,
    ticket: Ticket,
    written: WorkflowState,
    classification: Classification,
) -> None:
    # Only a confident label becomes the ticket's category. A low-confidence guess is kept
    # in the audit event, but the ticket stays uncategorised until clarified.
    confident = (
        classification.category != "unclear"
        and classification.confidence >= settings.classifier_confidence_threshold
    )
    if confident:
        await tickets.set_category(session, ticket, TicketCategory(classification.category))
    await audit.record(
        session,
        run_id=run.id,
        actor="classifier",
        event_type="run.classified",
        payload={
            "category": classification.category,
            "confidence": round(classification.confidence, 2),
            "order_hint": written.get("order_hint", OrderHint.NONE).value,
        },
    )


def _clarification_reason(final: WorkflowState) -> str:
    asked_for = final.get("asked_for")
    if asked_for == AskedFor.ORDER_NUMBER:  # confident label, but no verified order number
        return "missing_order_number"
    if asked_for == AskedFor.MORE_INFORMATION:  # the resolution asked the customer a question
        return "needs_more_information"
    classification = final.get("classification")
    if classification is not None and classification.category == "unclear":
        return "unclear_category"
    return "low_confidence"


async def _record_tool_calls(
    session: AsyncSession, run: WorkflowRun, actor: str, trace: list[ToolCallRecord]
) -> None:
    for call in trace:
        payload: dict[str, object] = {
            "tool": call.tool,
            "call_id": call.call_id,
            "outcome": call.outcome,
            "latency_ms": call.latency_ms,
        }
        if call.order_number is not None:  # set only for successful calls
            payload["order_number"] = call.order_number
        await audit.record(
            session, run_id=run.id, actor=actor, event_type="tool.called", payload=payload
        )


def _call_counts(trace: list[ToolCallRecord]) -> dict[str, int]:
    return {
        "tool_calls": len(trace),
        "failed_calls": sum(1 for call in trace if call.outcome != OUTCOME_OK),
    }


async def _record_gathering(
    session: AsyncSession, run: WorkflowRun, written: WorkflowState
) -> None:
    """Audit the supervisor and the specialists: names, outcome codes, latency and counts."""
    plan = written.get("supervisor")
    if plan is not None:
        await audit.record(
            session,
            run_id=run.id,
            actor="supervisor",
            event_type="supervisor.decided",
            payload={
                "run_investigator": plan.run_investigator,
                "run_policy": plan.run_policy,
                "added_by_code": plan.added_by_code,
                "extra_investigation_round": written.get("investigation_retry") is not None,
            },
        )

    investigation = written.get("investigation")
    if investigation is None:
        return
    if run.mode == RunMode.BASELINE:
        await _record_tool_calls(session, run, "baseline_agent", investigation.trace)
        await audit.record(
            session,
            run_id=run.id,
            actor="baseline_agent",
            event_type="baseline.completed",
            payload=_call_counts(investigation.trace),
        )
        return

    await audit.record(
        session,
        run_id=run.id,
        actor="investigator",
        event_type="investigation.started",
        payload={"run_id": str(run.id)},
    )
    await _record_tool_calls(session, run, "investigator", investigation.trace)
    evidence = investigation.evidence
    if evidence is not None:  # the investigation finished (it did not stop early)
        await audit.record(
            session,
            run_id=run.id,
            actor="investigator",
            event_type="investigation.completed",
            payload={
                **_call_counts(investigation.trace),
                "facts": len(evidence.facts),
                "dropped_facts": len(evidence.dropped_facts),
                "missing_information": len(evidence.missing_information),
            },
        )

    policy = written.get("policy")
    if policy is None:
        return
    await _record_tool_calls(session, run, "policy_agent", policy.trace)
    findings = policy.findings
    if findings is not None:
        await audit.record(
            session,
            run_id=run.id,
            actor="policy_agent",
            event_type="policy.completed",
            payload={
                **_call_counts(policy.trace),
                "searches": len(policy.searches),
                "rules": len(findings.rules),
                "dropped_rules": len(findings.dropped_rules),
                "policy_versions": findings.policy_versions,
                "no_relevant_policy": findings.no_relevant_policy,
                "conflicting_passages": findings.conflicting_passages,
            },
        )


async def _record_resolution(
    session: AsyncSession, run: WorkflowRun, written: WorkflowState
) -> None:
    outcome = written.get("outcome")
    if outcome is None:
        return
    payload: dict[str, object] = {
        "action": outcome.action.value,
        "reason_codes": outcome.reason_codes,
        "attempts": len(outcome.attempts),
        "critic_verdicts": [
            a.verdict.verdict if a.verdict else "not_run" for a in outcome.attempts
        ],
        "validation_codes": [[code.value for code in a.validation] for a in outcome.attempts],
        "failure": None if outcome.failure is None else outcome.failure.value,
    }
    if outcome.amount_minor is not None:
        payload["amount_minor"] = outcome.amount_minor
    await audit.record(
        session,
        run_id=run.id,
        actor="resolution_agent",
        event_type="resolution.completed",
        payload=payload,
    )


async def _record_usage(session: AsyncSession, run: WorkflowRun, meter: UsageMeter) -> None:
    """Add this segment's usage to the run's totals; the event holds this segment's numbers."""
    await workflow_runs.add_usage(
        session,
        run,
        calls=meter.calls,
        input_tokens=meter.input_tokens,
        output_tokens=meter.output_tokens,
    )
    await audit.record(
        session,
        run_id=run.id,
        actor="system",
        event_type="run.usage",
        payload={
            "llm_calls": meter.calls,
            "input_tokens": meter.input_tokens,
            "output_tokens": meter.output_tokens,
        },
    )
