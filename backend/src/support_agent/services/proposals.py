"""Turn a ticket into an immutable refund proposal with a pending approval (FR-13, FR-14).

No money moves here. The amount comes only from the eligibility rules, and the
customer comes only from the ticket row (which was created by the authenticated
customer), never from message text.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.domain.eligibility import check_eligibility
from support_agent.domain.enums import RunStatus, TicketStatus
from support_agent.domain.proposals import build_refund_payload, payload_hash
from support_agent.repositories import audit, proposals, tickets, workflow_runs
from support_agent.schemas.approvals import ProposalResponse
from support_agent.schemas.auth import Principal
from support_agent.services import eligibility
from support_agent.services.case_updates import set_ticket_status
from support_agent.services.tickets import ticket_not_found

ACTOR = "reviewer"


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

    payload = build_refund_payload(
        order_id=loaded.order_id,
        order_number=loaded.order_number,
        customer_id=ticket.customer_id,
        currency=loaded.currency,
        decision=decision,
    ).to_json_dict()
    digest = payload_hash(payload)

    run = await workflow_runs.create_manual(
        session, ticket_id=ticket.id, status=RunStatus.AWAITING_APPROVAL, now=now
    )
    proposal = await proposals.create_proposal(
        session, run_id=run.id, payload=payload, digest=digest
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
        run_id=run.id,
        actor=ACTOR,
        event_type="proposal.created",
        payload={
            "proposal_id": str(proposal.id),
            "ticket_id": str(ticket.id),
            "order_id": str(loaded.order_id),
            "amount_minor": payload["amount_minor"],
            "currency": payload["currency"],
            "payload_hash": digest,
            "reviewer_id": str(reviewer.id),
        },
    )
    await audit.record(
        session,
        run_id=run.id,
        actor=ACTOR,
        event_type="approval.requested",
        payload={
            "approval_id": str(approval.id),
            "proposal_id": str(proposal.id),
            "expires_at": approval.expires_at.isoformat(),
        },
    )
    await set_ticket_status(session, ticket, TicketStatus.IN_PROGRESS, run_id=run.id, actor=ACTOR)
    await session.commit()

    return ProposalResponse(
        proposal_id=proposal.id,
        approval_id=approval.id,
        run_id=run.id,
        ticket_id=ticket.id,
        order_number=loaded.order_number,
        amount_minor=payload["amount_minor"],
        currency=payload["currency"],
        payload_hash=digest,
        status=proposal.status,
        expires_at=approval.expires_at,
    )
