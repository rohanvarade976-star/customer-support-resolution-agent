"""Shared steps for one refund case: a proposal, its workflow run and its ticket.

- load_case fetches all three together.
- set_run_status / set_ticket_status change a status and record the audit event, so no
  change can happen without its event.

Nothing is committed here; the caller commits together with its own changes.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.errors import AppError
from support_agent.db.models import ActionProposal, Ticket, WorkflowRun
from support_agent.domain.enums import RunStatus, TicketStatus
from support_agent.repositories import audit, proposals, tickets, workflow_runs

# Only finishing a run is worth an event; the in-between states have their own events.
_RUN_EVENTS = {
    RunStatus.COMPLETED: "run.completed",
    RunStatus.ESCALATED: "run.escalated",
    RunStatus.FAILED: "run.failed",
}


async def load_case(
    session: AsyncSession, proposal_id: UUID
) -> tuple[ActionProposal, WorkflowRun, Ticket]:
    proposal = await proposals.get_proposal(session, proposal_id)
    if proposal is None:
        raise AppError(404, "PROPOSAL_NOT_FOUND", "Proposal not found.")
    run = await workflow_runs.get(session, proposal.run_id)
    record = None if run is None else await tickets.get_by_id(session, run.ticket_id)
    if run is None or record is None:  # foreign keys make this impossible
        raise AppError(500, "INTERNAL_ERROR", "Proposal is missing its run or ticket.")
    return proposal, run, record.ticket


async def set_ticket_status(
    session: AsyncSession, ticket: Ticket, status: TicketStatus, *, run_id: UUID, actor: str
) -> None:
    if ticket.status == status:
        return
    await tickets.set_status(session, ticket, status)
    await audit.record(
        session,
        run_id=run_id,
        actor=actor,
        event_type="ticket.updated",
        payload={"ticket_id": str(ticket.id), "status": status.value},
    )


async def set_run_status(
    session: AsyncSession,
    run: WorkflowRun,
    status: RunStatus,
    *,
    now: datetime,
    actor: str,
    details: dict[str, Any] | None = None,  # extra redacted fields for the event payload
) -> None:
    if run.status == status:
        return
    await workflow_runs.set_status(session, run, status, now)
    event = _RUN_EVENTS.get(status)
    if event is not None:
        await audit.record(
            session,
            run_id=run.id,
            actor=actor,
            event_type=event,
            payload={"run_id": str(run.id), "status": status.value, **(details or {})},
        )
