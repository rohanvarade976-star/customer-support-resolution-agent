"""Starting and reading workflow runs.

start_run:
1. Check the caller owns the ticket (anyone else's ticket is a 404, like a missing one).
2. Refuse if the ticket already has an unfinished run (409).
3. Create the run, record `run.created`, commit.
4. Run the graph synchronously (a background worker takes this over in M7), counting the
   run's LLM calls and tokens.
5. Save what the graph decided: ticket category and reply, run and ticket status, audit
   events, usage. A refund proposal and its pending approval are created inside the graph
   (through `propose_refund` below) and committed here, together with everything else.
If anything unexpected breaks, the run is marked `failed` so it does not stay open, and
anything the graph wrote is rolled back.

The customer id given to the workflow is the ticket's owner, which step 1 has just
matched to the JWT. It never comes from the complaint text or the model.

Audit payloads hold ids, labels, numbers and codes only. The classifier's `reason`, a
rejected order number, the supervisor's hints, the investigator's plan, facts and tool
results, policy text and the drafted message are all left out. Events: `run.classified`,
`supervisor.decided`, `investigation.started`, one `tool.called` per call,
`investigation.completed`, `policy.completed`, `resolution.completed`, `proposal.created`
(written by services/proposals.py), the run's end event, and `run.usage`.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.db.models import Ticket, WorkflowRun
from support_agent.domain.enums import (
    Role,
    RunMode,
    RunStatus,
    TicketCategory,
    TicketStatus,
)
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import LLMClient
from support_agent.llm.tool_loop import OUTCOME_OK, ToolCallRecord
from support_agent.llm.usage import UsageMeter, metered
from support_agent.rag.embeddings import Embedder
from support_agent.repositories import audit, tickets, workflow_runs
from support_agent.schemas.auth import Principal
from support_agent.schemas.runs import RunResponse
from support_agent.services import eligibility, order_status
from support_agent.services import proposals as proposal_service
from support_agent.services.case_updates import set_run_status, set_ticket_status
from support_agent.services.orders import customer_owns_order
from support_agent.services.tickets import ticket_not_found
from support_agent.tools.base import ToolContext
from support_agent.tools.read_tools import build_read_registry
from support_agent.workflow.deps import BranchSessions, CaseFacts, WorkflowDeps
from support_agent.workflow.graph import initial_state, run_workflow
from support_agent.workflow.state import OrderHint, WorkflowState

log = logging.getLogger(__name__)

# Where the ticket stands once the run stops at each end state.
_TICKET_STATUS_FOR_RUN = {
    RunStatus.AWAITING_CUSTOMER: TicketStatus.AWAITING_CUSTOMER,
    RunStatus.AWAITING_APPROVAL: TicketStatus.IN_PROGRESS,
    RunStatus.COMPLETED: TicketStatus.RESOLVED,
    RunStatus.ESCALATED: TicketStatus.ESCALATED,
}


def run_not_found() -> AppError:
    return AppError(404, "RUN_NOT_FOUND", "Run not found.")


def _to_response(run: WorkflowRun, ticket: Ticket) -> RunResponse:
    return RunResponse(
        run_id=run.id,
        ticket_id=ticket.id,
        status=run.status,
        category=ticket.category,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


async def start_run(
    session: AsyncSession,
    principal: Principal,
    ticket_id: UUID,
    *,
    llm: LLMClient,
    settings: Settings,
    shipping_client: ShippingClient,
    embedder: Embedder,
    clock: Callable[[], datetime],
    branch_sessions: BranchSessions,
    mode: RunMode = RunMode.MULTI_AGENT,
) -> RunResponse:
    record = await tickets.get_by_id(session, ticket_id)
    if record is None or record.ticket.customer_id != principal.id:
        raise ticket_not_found()
    ticket = record.ticket
    if await workflow_runs.has_unfinished_run(session, ticket.id):
        raise AppError(409, "RUN_ALREADY_ACTIVE", "This ticket already has a run in progress.")

    run = await workflow_runs.create_agent_run(session, ticket_id=ticket.id, mode=mode, now=clock())
    await audit.record(
        session,
        run_id=run.id,
        actor="customer",
        event_type="run.created",
        payload={"run_id": str(run.id), "ticket_id": str(ticket.id)},
    )
    await session.commit()

    async def owns_order(customer_id: UUID, order_number: str) -> bool:
        return await customer_owns_order(session, customer_id, order_number)

    async def load_case_facts(customer_id: UUID, order_number: str, now: datetime) -> CaseFacts:
        decision = await eligibility.check_refund_eligibility(
            session, customer_id, order_number, now
        )
        threshold = await order_status.get_investigation_threshold(
            session, customer_id, order_number
        )
        return CaseFacts(decision, threshold)

    async def propose_refund(order_number: str, amount_minor: int) -> UUID:
        return await proposal_service.create_proposal_for_run(
            session,
            ticket,
            run.id,
            order_number=order_number,
            expected_amount_minor=amount_minor,
            now=clock(),
            settings=settings,
        )

    # The tools' customer is the ticket's owner, already matched to the JWT above.
    tool_context = ToolContext(
        session, ticket.customer_id, settings, shipping_client, embedder, clock=clock
    )
    deps = WorkflowDeps(
        llm=llm,
        owns_order=owns_order,
        confidence_threshold=settings.classifier_confidence_threshold,
        registry=build_read_registry(),
        tool_context=tool_context,
        max_tool_calls=settings.investigator_max_tool_calls,
        branch_sessions=branch_sessions,
        load_case_facts=load_case_facts,
        propose_refund=propose_refund,
        mode=mode,
    )
    state = initial_state(
        run_id=run.id,
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        complaint_text=ticket.summary,
        ticket_order_number=record.order_number,
    )
    try:
        with metered() as meter:
            final = await run_workflow(deps, state)
        await _save_outcome(session, settings, run, ticket, final, meter, clock())
        await session.commit()
    except Exception:
        await _mark_failed(session, run, clock())
        raise

    log.info("run_finished", extra={"run_id": str(run.id), "status": run.status.value})
    return _to_response(run, ticket)


async def _save_outcome(
    session: AsyncSession,
    settings: Settings,
    run: WorkflowRun,
    ticket: Ticket,
    final: WorkflowState,
    meter: UsageMeter,
    now: datetime,
) -> None:
    classification = final.get("classification")
    status = final["status"]
    if classification is not None:
        # Only a confident label becomes the ticket's category. A low-confidence guess is
        # kept in the audit event, but the ticket stays uncategorised until clarified.
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
                "order_hint": final.get("order_hint", OrderHint.NONE).value,
            },
        )

    await _record_gathering(session, run, final)
    await _record_resolution(session, run, final)

    ticket_status = _TICKET_STATUS_FOR_RUN[status]  # KeyError = a graph bug -> run failed
    details: dict[str, object] = {}
    if final["errors"]:
        details["error_codes"] = [e.value for e in final["errors"]]
    if "proposal_refusal" in final:
        details["proposal_refusal"] = final["proposal_refusal"]
    await set_run_status(session, run, status, now=now, actor="system", details=details or None)

    if status == RunStatus.AWAITING_CUSTOMER and classification is not None:
        await audit.record(
            session,
            run_id=run.id,
            actor="system",
            event_type="clarification.requested",
            payload={"reason": _clarification_reason(final, classification.category)},
        )
    if reply := final.get("customer_reply"):
        await tickets.set_reply(session, ticket, reply)
    await set_ticket_status(session, ticket, ticket_status, run_id=run.id, actor="system")
    await _record_usage(session, run, meter)


def _clarification_reason(final: WorkflowState, category: str) -> str:
    if category == "unclear":
        return "unclear_category"
    if final.get("missing_information"):  # confident label, but no verified order number
        return "missing_order_number"
    if "outcome" in final:  # the resolution asked the customer a question
        return "needs_more_information"
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


async def _record_gathering(session: AsyncSession, run: WorkflowRun, final: WorkflowState) -> None:
    """Audit the supervisor and the specialists: names, outcome codes, latency and counts."""
    plan = final.get("supervisor")
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
                "extra_investigation_round": "investigation_retry" in final,
            },
        )

    investigation = final.get("investigation")
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

    policy = final.get("policy")
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


async def _record_resolution(session: AsyncSession, run: WorkflowRun, final: WorkflowState) -> None:
    outcome = final.get("outcome")
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
    await workflow_runs.set_usage(
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


async def _mark_failed(session: AsyncSession, run: WorkflowRun, now: datetime) -> None:
    """Record that the run crashed, in a fresh transaction, so it never stays open."""
    await session.rollback()
    await session.refresh(run)  # rollback expired it; reload before changing it
    await set_run_status(
        session,
        run,
        RunStatus.FAILED,
        now=now,
        actor="system",
        details={"error_codes": ["INTERNAL_ERROR"]},
    )
    await session.commit()


async def get_run(session: AsyncSession, principal: Principal, run_id: UUID) -> RunResponse:
    run = await workflow_runs.get(session, run_id)
    record = None if run is None else await tickets.get_by_id(session, run.ticket_id)
    if run is None or record is None:
        raise run_not_found()
    owns_it = principal.role == Role.CUSTOMER and record.ticket.customer_id == principal.id
    is_staff = principal.role in (Role.REVIEWER, Role.ADMIN)
    if not (owns_it or is_staff):
        raise run_not_found()  # same answer as "no such run"
    return _to_response(run, record.ticket)
