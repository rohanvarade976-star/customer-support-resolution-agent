"""The reviewer's escalations inbox: escalated tickets, and what a person can do about them
(ADR-022). Reviewers only: the routes check the role before anything here runs.

- list and detail: escalated tickets whose latest run escalated, with plain reason codes
  computed by code from the audit trail (domain/escalations.py);
- reply: a reviewer's message to the customer. It passes the same output guardrail as every
  run's reply (domain/output_guard.py); a failing reply is refused with the issue codes, so
  the reviewer can fix it. It is stored as the ticket's verified reply, marked "specialist",
  and audited with ids and its length only. The text is never sent to a model;
- resolve: closes the ticket. Only after a specialist reply, so the customer is never left
  with "a person will follow up" on a closed ticket. The note goes into the audit trail only.

A manual refund proposal uses the existing reviewer endpoint (services/proposals.py), with
the same eligibility, approval and hash rules; another reviewer must decide it.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.db.models import Ticket, WorkflowRun
from support_agent.domain.enums import ReplyAuthor, RunStatus, TicketStatus
from support_agent.domain.escalations import escalation_reasons
from support_agent.domain.output_guard import check_reply
from support_agent.domain.run_analysis import ESCALATION_EVENT_TYPES, EventView
from support_agent.repositories import audit, tickets, workflow_runs
from support_agent.repositories.tickets import TicketRecord
from support_agent.schemas.auth import Principal
from support_agent.schemas.escalations import (
    EscalationActionResponse,
    EscalationCase,
    EscalationItem,
    EscalationListResponse,
    EscalationReasonView,
    ResolveRequest,
    SpecialistReplyRequest,
)
from support_agent.services import orders as orders_service
from support_agent.services.approval_cases import governing_policy
from support_agent.services.case_readiness import case_facts, load_order_or_none
from support_agent.services.case_updates import set_ticket_status

ACTOR = "reviewer"


def escalation_not_found() -> AppError:
    return AppError(404, "ESCALATION_NOT_FOUND", "Escalated ticket not found.")


def _escalated_at(run: WorkflowRun, ticket: Ticket) -> datetime:
    return run.completed_at or ticket.updated_at


async def _reasons(
    session: AsyncSession, settings: Settings, run_ids: list[UUID]
) -> dict[UUID, list[EscalationReasonView]]:
    """Each run's reasons, from one query over the audit trail (no checkpoint reads)."""
    events = await audit.list_of_types_for_runs(session, run_ids, ESCALATION_EVENT_TYPES)
    by_run: dict[UUID, list[EventView]] = {run_id: [] for run_id in run_ids}
    for event in events:
        if event.run_id is not None:
            by_run[event.run_id].append(
                EventView(event.event_type, event.actor, event.created_at, event.redacted_payload)
            )
    threshold = settings.classifier_confidence_threshold
    return {
        run_id: [
            EscalationReasonView(code=code, codes=codes)
            for code, codes in escalation_reasons(views, threshold)
        ]
        for run_id, views in by_run.items()
    }


def _item(
    record: TicketRecord, run: WorkflowRun, reasons: list[EscalationReasonView]
) -> EscalationItem:
    ticket = record.ticket
    return EscalationItem(
        ticket_id=ticket.id,
        run_id=run.id,
        customer_name=record.customer_name,
        order_number=record.order_number,
        category=ticket.category,
        complaint=ticket.summary,
        escalated_at=_escalated_at(run, ticket),
        reasons=reasons,
        specialist_replied=ticket.reply_author == ReplyAuthor.SPECIALIST,
    )


async def list_escalations(session: AsyncSession, settings: Settings) -> EscalationListResponse:
    records = await tickets.list_escalated(session)
    runs = await workflow_runs.latest_for_tickets(session, [r.ticket.id for r in records])
    # Only tickets whose latest run escalated: a manual proposal's run moves a ticket on.
    pairs = [
        (record, runs[record.ticket.id])
        for record in records
        if record.ticket.id in runs and runs[record.ticket.id].status == RunStatus.ESCALATED
    ]
    reasons = await _reasons(session, settings, [run.id for _record, run in pairs])
    items = [_item(record, run, reasons[run.id]) for record, run in pairs]
    items.sort(key=lambda item: item.escalated_at, reverse=True)  # newest escalation first
    return EscalationListResponse(items=items)


async def _load(session: AsyncSession, ticket_id: UUID) -> tuple[TicketRecord, WorkflowRun]:
    """A ticket whose latest run escalated; 404 otherwise (including after a new run)."""
    record = await tickets.get_by_id(session, ticket_id)
    runs = await workflow_runs.latest_for_tickets(session, [ticket_id])
    run = runs.get(ticket_id)
    if record is None or run is None or run.status != RunStatus.ESCALATED:
        raise escalation_not_found()
    return record, run


async def get_escalation(
    session: AsyncSession, settings: Settings, ticket_id: UUID, now: datetime
) -> EscalationCase:
    record, run = await _load(session, ticket_id)
    ticket = record.ticket
    reasons = await _reasons(session, settings, [run.id])
    loaded = (
        None
        if record.order_number is None
        else await load_order_or_none(session, ticket.customer_id, record.order_number)
    )
    return EscalationCase(
        **_item(record, run, reasons[run.id]).model_dump(),
        ticket_status=ticket.status,
        reply=ticket.customer_reply,
        reply_from=ticket.reply_author,
        facts=None if loaded is None else case_facts(loaded, now),
        currency=None if loaded is None else loaded.currency,
        governing_policy=None if loaded is None else governing_policy(loaded),
    )


async def _load_open(
    session: AsyncSession, ticket_id: UUID
) -> tuple[Ticket, TicketRecord, WorkflowRun]:
    """The ticket locked until commit, so two reviewers' actions on it happen one at a time."""
    record, run = await _load(session, ticket_id)
    ticket = await tickets.lock(session, ticket_id)
    if ticket is None:  # deleted in between: cannot happen, tickets are never deleted
        raise escalation_not_found()
    if ticket.status != TicketStatus.ESCALATED:
        raise AppError(409, "TICKET_NOT_ESCALATED", "This ticket is no longer escalated.")
    return ticket, record, run


async def reply(
    session: AsyncSession, reviewer: Principal, ticket_id: UUID, body: SpecialistReplyRequest
) -> EscalationActionResponse:
    ticket, _record, run = await _load_open(session, ticket_id)
    owned = await orders_service.list_order_numbers(session, ticket.customer_id)
    issues = check_reply(body.text, owned)
    if issues:
        # Refused, not replaced: the reviewer sees why and fixes the text. Nothing is stored.
        raise AppError(
            422,
            "REPLY_REJECTED",
            "This reply cannot be sent as written.",
            details={"codes": [issue.value for issue in issues]},
        )
    await tickets.set_reply(session, ticket, body.text, author=ReplyAuthor.SPECIALIST)
    await audit.record(
        session,
        run_id=run.id,
        actor=ACTOR,
        event_type="ticket.specialist_replied",
        # Ids and a length only: the reply itself is on the ticket, never in the audit trail.
        payload={
            "ticket_id": str(ticket.id),
            "reviewer_id": str(reviewer.id),
            "reply_length": len(body.text),
        },
    )
    await session.commit()
    return EscalationActionResponse(
        ticket_id=ticket.id, ticket_status=ticket.status, reply_from=ticket.reply_author
    )


async def resolve(
    session: AsyncSession, reviewer: Principal, ticket_id: UUID, body: ResolveRequest
) -> EscalationActionResponse:
    ticket, _record, run = await _load_open(session, ticket_id)
    if ticket.reply_author != ReplyAuthor.SPECIALIST:
        raise AppError(
            409,
            "REPLY_REQUIRED",
            "Send the customer a reply before resolving, so they know the outcome.",
        )
    await set_ticket_status(session, ticket, TicketStatus.RESOLVED, run_id=run.id, actor=ACTOR)
    await audit.record(
        session,
        run_id=run.id,
        actor=ACTOR,
        event_type="ticket.resolved",
        # "note" is not in REVIEWER_PAYLOAD_KEYS, so it is never streamed or shown.
        payload={"ticket_id": str(ticket.id), "reviewer_id": str(reviewer.id), "note": body.note},
    )
    await session.commit()
    return EscalationActionResponse(
        ticket_id=ticket.id, ticket_status=ticket.status, reply_from=ticket.reply_author
    )
