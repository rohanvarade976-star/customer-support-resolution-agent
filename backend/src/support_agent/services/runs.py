"""Starting and reading workflow runs.

start_run:
1. Check the caller owns the ticket (anyone else's ticket is a 404, like a missing one).
2. Refuse if the ticket already has an unfinished run (409).
3. Create the run, record `run.created`, commit.
4. Run the graph synchronously (a background worker takes this over in M7).
5. Save what the graph decided: ticket category, run and ticket status, audit events.
If anything unexpected breaks, the run is marked `failed` so it does not stay open.

The customer id given to the workflow is the ticket's owner, which step 1 has just
matched to the JWT. It never comes from the complaint text or the model.

Audit payloads hold ids, labels, numbers and codes only. The classifier's `reason` and a
rejected order number are left out: the first can quote the customer, the second may be
someone else's order. The investigator's plan, facts and tool results are left out too.
Its events are `investigation.started`, one `tool.called` per call (tool, call id, outcome
code, latency, and the order number only when the call succeeded) and, if it finished,
`investigation.completed` with counts.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.db.models import Ticket, WorkflowRun
from support_agent.domain.enums import Role, RunMode, RunStatus, TicketCategory, TicketStatus
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import LLMClient
from support_agent.llm.tool_loop import OUTCOME_OK
from support_agent.rag.embeddings import Embedder
from support_agent.repositories import audit, tickets, workflow_runs
from support_agent.schemas.auth import Principal
from support_agent.schemas.runs import RunResponse
from support_agent.services.case_updates import set_run_status, set_ticket_status
from support_agent.services.orders import customer_owns_order
from support_agent.services.tickets import ticket_not_found
from support_agent.tools.base import ToolContext
from support_agent.tools.read_tools import build_read_registry
from support_agent.workflow.graph import WorkflowDeps, initial_state, run_workflow
from support_agent.workflow.state import OrderHint, WorkflowState

log = logging.getLogger(__name__)

# Where the ticket stands once the run stops at each (M5) end state.
_TICKET_STATUS_FOR_RUN = {
    RunStatus.INVESTIGATING: TicketStatus.IN_PROGRESS,
    RunStatus.AWAITING_CUSTOMER: TicketStatus.AWAITING_CUSTOMER,
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
) -> RunResponse:
    record = await tickets.get_by_id(session, ticket_id)
    if record is None or record.ticket.customer_id != principal.id:
        raise ticket_not_found()
    ticket = record.ticket
    if await workflow_runs.has_unfinished_run(session, ticket.id):
        raise AppError(409, "RUN_ALREADY_ACTIVE", "This ticket already has a run in progress.")

    run = await workflow_runs.create_agent_run(
        session, ticket_id=ticket.id, mode=RunMode.MULTI_AGENT, now=clock()
    )
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
    )
    state = initial_state(
        run_id=run.id,
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        complaint_text=ticket.summary,
        ticket_order_number=record.order_number,
    )
    try:
        final = await run_workflow(deps, state)
        await _save_outcome(session, run, ticket, final, clock())
        await session.commit()
    except Exception:
        await _mark_failed(session, run, clock())
        raise

    log.info("run_finished", extra={"run_id": str(run.id), "status": run.status.value})
    return _to_response(run, ticket)


async def _save_outcome(
    session: AsyncSession, run: WorkflowRun, ticket: Ticket, final: WorkflowState, now: datetime
) -> None:
    classification = final.get("classification")
    status = final["status"]
    # The investigator only runs after a confident label, even if it then fails.
    investigated = "tool_trace" in final
    if classification is not None:
        # Only a confident label becomes the ticket's category. A low-confidence guess is
        # kept in the audit event, but the ticket stays uncategorised until clarified.
        confident = status == RunStatus.INVESTIGATING or investigated
        if confident and classification.category != "unclear":
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

    if investigated:
        await _record_investigation(session, run, final)

    ticket_status = _TICKET_STATUS_FOR_RUN[status]  # KeyError = a graph bug -> run failed
    details = {"error_codes": [e.value for e in final["errors"]]} if final["errors"] else None
    await set_run_status(session, run, status, now=now, actor="system", details=details)

    if status == RunStatus.AWAITING_CUSTOMER and classification is not None:
        await audit.record(
            session,
            run_id=run.id,
            actor="system",
            event_type="clarification.requested",
            payload={"reason": _clarification_reason(final, classification.category)},
        )
    await set_ticket_status(session, ticket, ticket_status, run_id=run.id, actor="system")


def _clarification_reason(final: WorkflowState, category: str) -> str:
    if category == "unclear":
        return "unclear_category"
    if final.get("missing_information"):  # confident label, but no verified order number
        return "missing_order_number"
    return "low_confidence"


async def _record_investigation(
    session: AsyncSession, run: WorkflowRun, final: WorkflowState
) -> None:
    """Audit the investigator: names, outcome codes, latency and counts. No content."""
    trace = final.get("tool_trace", [])
    await audit.record(
        session,
        run_id=run.id,
        actor="investigator",
        event_type="investigation.started",
        payload={"run_id": str(run.id)},
    )
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
            session, run_id=run.id, actor="investigator", event_type="tool.called", payload=payload
        )
    evidence = final.get("evidence")
    if evidence is not None:  # the investigation finished (it did not stop early)
        await audit.record(
            session,
            run_id=run.id,
            actor="investigator",
            event_type="investigation.completed",
            payload={
                "tool_calls": len(trace),
                "failed_calls": sum(1 for call in trace if call.outcome != OUTCOME_OK),
                "facts": len(evidence.facts),
                "dropped_facts": len(evidence.dropped_facts),
                "missing_information": len(evidence.missing_information),
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
