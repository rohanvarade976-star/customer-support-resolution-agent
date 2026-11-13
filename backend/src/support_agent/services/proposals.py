"""Turn a ticket into an immutable refund proposal with a pending approval (FR-13, FR-14).

No money moves here. The amount comes only from the eligibility rules, and the
customer comes only from the ticket row (which was created by the authenticated
customer), never from message text.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.db.models import ActionProposal, Approval, Ticket
from support_agent.domain.eligibility import EligibilityDecision, check_eligibility
from support_agent.domain.enums import RunStatus, TicketStatus
from support_agent.domain.proposals import build_refund_payload, payload_hash
from support_agent.repositories import audit, proposals, tickets, workflow_runs
from support_agent.schemas.approvals import ProposalResponse
from support_agent.schemas.auth import Principal
from support_agent.services import eligibility
from support_agent.services.case_updates import set_ticket_status
from support_agent.services.eligibility import LoadedOrder
from support_agent.services.tickets import ticket_not_found

ACTOR = "reviewer"


@dataclass(frozen=True)
class _Stored:
    proposal: ActionProposal
    approval: Approval
    payload: dict[str, Any]
    digest: str


async def _store_proposal(
    session: AsyncSession,
    *,
    run_id: UUID,
    ticket: Ticket,
    loaded: LoadedOrder,
    decision: EligibilityDecision,
    now: datetime,
    settings: Settings,
    actor: str,
    extra_audit: dict[str, Any],
) -> _Stored:
    """The immutable payload, its hash, the proposal, the pending approval and their events.

    Shared by the reviewer endpoint and the workflow, so both store exactly the same thing.
    The amount comes from `decision` only. Nothing is committed here.
    """
    payload = build_refund_payload(
        order_id=loaded.order_id,
        order_number=loaded.order_number,
        customer_id=ticket.customer_id,
        currency=loaded.currency,
        decision=decision,
    ).to_json_dict()
    digest = payload_hash(payload)
    proposal = await proposals.create_proposal(
        session, run_id=run_id, payload=payload, digest=digest
    )
    approval = await proposals.create_approval(
        session,
        proposal_id=proposal.id,
        digest=digest,
        created_at=now,
        expires_at=now + timedelta(minutes=settings.approval_ttl_minutes),
    )
    await audit.record(
        session,
        run_id=run_id,
        actor=actor,
        event_type="proposal.created",
        payload={
            "proposal_id": str(proposal.id),
            "ticket_id": str(ticket.id),
            "order_id": str(loaded.order_id),
            "amount_minor": payload["amount_minor"],
            "currency": payload["currency"],
            "payload_hash": digest,
            **extra_audit,
        },
    )
    await audit.record(
        session,
        run_id=run_id,
        actor=actor,
        event_type="approval.requested",
        payload={
            "approval_id": str(approval.id),
            "proposal_id": str(proposal.id),
            "expires_at": approval.expires_at.isoformat(),
        },
    )
    return _Stored(proposal, approval, payload, digest)


async def create_proposal_for_run(
    session: AsyncSession,
    ticket: Ticket,
    run_id: UUID,
    *,
    order_number: str,
    expected_amount_minor: int,
    now: datetime,
    settings: Settings,
) -> UUID:
    """The workflow's refund proposal: a pending approval for the run's own order.

    The agents have only proposed. Here code recomputes eligibility from the database and
    refuses (AppError, nothing stored) unless the order is still eligible for exactly the
    amount the workflow validated and no proposal for it is already open.

    If THIS run already has an open proposal for the order, its id is returned and nothing
    new is stored: the step ran before, and the worker died before the graph saved it
    (ADR-012). Nothing is committed here: the workflow's `propose_refund` dependency commits
    straight after, before the graph's checkpoint can point at the proposal.
    """
    loaded = await eligibility.load_order(session, ticket.customer_id, order_number)
    existing = await proposals.open_proposal_for_run(session, run_id, loaded.order_id, now)
    if existing is not None:
        return existing.id
    decision = check_eligibility(loaded.facts, now)
    if not decision.eligible:
        raise AppError(409, "NOT_ELIGIBLE", "This order is not eligible for a refund.")
    if decision.refundable_amount_minor != expected_amount_minor:
        raise AppError(409, "AMOUNT_CHANGED", "The refundable amount changed during the run.")
    if await proposals.order_has_open_proposal(session, loaded.order_id, now):
        raise AppError(
            409, "PROPOSAL_ALREADY_PENDING", "A refund proposal for this order awaits a decision."
        )
    stored = await _store_proposal(
        session,
        run_id=run_id,
        ticket=ticket,
        loaded=loaded,
        decision=decision,
        now=now,
        settings=settings,
        actor="system",
        extra_audit={},
    )
    return stored.proposal.id


async def create_refund_proposal(
    session: AsyncSession,
    reviewer: Principal,
    ticket_id: UUID,
    *,
    now: datetime,
    settings: Settings,
) -> ProposalResponse:
    record = await tickets.get_by_id(session, ticket_id)
    if record is None:
        raise ticket_not_found()
    if record.order_number is None:
        raise AppError(409, "TICKET_HAS_NO_ORDER", "This ticket does not name an order.")
    ticket = record.ticket

    loaded = await eligibility.load_order(session, ticket.customer_id, record.order_number)
    decision = check_eligibility(loaded.facts, now)
    if not decision.eligible:
        reasons = [reason.value for reason in decision.reasons]
        await audit.record(
            session,
            run_id=None,  # no run is created for a refused proposal
            actor=ACTOR,
            event_type="proposal.refused",
            payload={
                "ticket_id": str(ticket.id),
                "order_id": str(loaded.order_id),
                "reasons": reasons,
                "reviewer_id": str(reviewer.id),
            },
        )
        await session.commit()
        raise AppError(
            409,
            "NOT_ELIGIBLE",
            "This order is not eligible for a refund.",
            details={"reasons": reasons},
        )

    if await proposals.order_has_open_proposal(session, loaded.order_id, now):
        raise AppError(
            409, "PROPOSAL_ALREADY_PENDING", "A refund proposal for this order awaits a decision."
        )

    try:
        run = await workflow_runs.create_manual(
            session, ticket_id=ticket.id, status=RunStatus.AWAITING_APPROVAL, now=now
        )
    except workflow_runs.RunAlreadyActiveError:
        await session.rollback()
        raise AppError(
            409, "RUN_ALREADY_ACTIVE", "This ticket already has a run in progress."
        ) from None
    stored = await _store_proposal(
        session,
        run_id=run.id,
        ticket=ticket,
        loaded=loaded,
        decision=decision,
        now=now,
        settings=settings,
        actor=ACTOR,
        extra_audit={"reviewer_id": str(reviewer.id)},
    )
    proposal, approval = stored.proposal, stored.approval
    await set_ticket_status(session, ticket, TicketStatus.IN_PROGRESS, run_id=run.id, actor=ACTOR)
    await session.commit()

    return ProposalResponse(
        proposal_id=proposal.id,
        approval_id=approval.id,
        run_id=run.id,
        ticket_id=ticket.id,
        order_number=loaded.order_number,
        amount_minor=stored.payload["amount_minor"],
        currency=stored.payload["currency"],
        payload_hash=stored.digest,
        status=proposal.status,
        expires_at=approval.expires_at,
    )
