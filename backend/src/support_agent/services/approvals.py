"""The reviewer's approval queue and decisions (FR-14, FR-15).

The reviewer's identity comes from the token (the Principal), never from the request
body. A decision is refused when the approval has expired, was already decided, or its
proposal no longer hashes to the approved value.
"""

from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.errors import AppError
from support_agent.db.models import ActionProposal, Approval, Ticket, WorkflowRun
from support_agent.domain.enums import ApprovalDecision, ProposalStatus, RunStatus, TicketStatus
from support_agent.domain.proposals import payload_hash
from support_agent.integrations.payments import PaymentClient
from support_agent.repositories import audit, proposals
from support_agent.schemas.approvals import (
    ApprovalListResponse,
    DecisionRequest,
    DecisionResponse,
    PendingApproval,
    RefundSummary,
)
from support_agent.schemas.auth import Principal
from support_agent.services import refund_execution
from support_agent.services.case_updates import load_case, set_run_status, set_ticket_status

ACTOR = "reviewer"
REJECTED_MESSAGE = "The refund proposal was rejected. The ticket is open again for follow-up."


async def list_pending(session: AsyncSession, now: datetime) -> ApprovalListResponse:
    items = [
        PendingApproval(
            approval_id=record.approval.id,
            proposal_id=record.proposal.id,
            ticket_id=record.ticket_id,
            order_number=record.order_number,
            customer_name=record.customer_name,
            amount_minor=record.proposal.payload["amount_minor"],
            currency=record.proposal.payload["currency"],
            reason=record.proposal.payload["reason"],
            reasons=record.proposal.payload["checks_passed"],
            created_at=record.approval.created_at,
            expires_at=record.approval.expires_at,
        )
        for record in await proposals.list_pending(session, now)
    ]
    return ApprovalListResponse(items=items)


async def decide(
    session: AsyncSession,
    payments: PaymentClient,
    reviewer: Principal,
    approval_id: UUID,
    body: DecisionRequest,
    *,
    now: datetime,
) -> DecisionResponse:
    # Locked until the decision is committed, so two reviewers cannot both decide.
    approval = await proposals.lock_approval(session, approval_id)
    if approval is None:
        raise AppError(404, "APPROVAL_NOT_FOUND", "Approval not found.")
    proposal, run, ticket = await load_case(session, approval.proposal_id)

    if approval.decision != ApprovalDecision.PENDING:
        raise AppError(409, "ALREADY_DECIDED", "This approval has already been decided.")
    if proposal.status != ProposalStatus.PENDING_APPROVAL:
        raise AppError(409, "PROPOSAL_NOT_PENDING", "This proposal is no longer awaiting review.")
    if now >= approval.expires_at:
        await _expire(session, approval, proposal, run, ticket, now)
    if payload_hash(proposal.payload) != approval.payload_hash:
        await _refuse_changed(session, proposal, run, ticket, now)

    reviewer_id = str(reviewer.id)
    if body.decision == "reject":
        return await _reject(session, approval, proposal, run, ticket, reviewer_id, body.note, now)

    await proposals.record_decision(
        session,
        approval,
        decision=ApprovalDecision.APPROVED,
        reviewer_id=reviewer_id,
        decided_at=now,
    )
    await proposals.set_proposal_status(session, proposal, ProposalStatus.APPROVED)
    await audit.record(
        session,
        run_id=run.id,
        actor=ACTOR,
        event_type="approval.approved",
        payload=_decision_payload(approval, reviewer_id, body.note),
    )
    await set_run_status(session, run, RunStatus.EXECUTING, now=now, actor=ACTOR)
    await session.commit()  # the decision is stored (and the lock released) before money moves

    operation = await refund_execution.execute_approved_refund(
        session, payments, proposal.id, now=now
    )
    return DecisionResponse(
        approval_id=approval.id,
        proposal_id=proposal.id,
        decision="approved",
        proposal_status=proposal.status,
        refund=RefundSummary(
            refund_id=operation.id,
            status=operation.status,
            amount_minor=operation.amount_minor,
            provider_reference=operation.provider_reference,
        ),
        message=refund_execution.describe(operation.status),
    )


def _decision_payload(approval: Approval, reviewer_id: str, note: str | None) -> dict[str, Any]:
    return {
        "approval_id": str(approval.id),
        "proposal_id": str(approval.proposal_id),
        "reviewer_id": reviewer_id,
        "note": note,
    }


async def _reject(
    session: AsyncSession,
    approval: Approval,
    proposal: ActionProposal,
    run: WorkflowRun,
    ticket: Ticket,
    reviewer_id: str,
    note: str | None,
    now: datetime,
) -> DecisionResponse:
    """No money moves. The run is done, but the ticket goes back to open for a human."""
    await proposals.record_decision(
        session,
        approval,
        decision=ApprovalDecision.REJECTED,
        reviewer_id=reviewer_id,
        decided_at=now,
    )
    await proposals.set_proposal_status(session, proposal, ProposalStatus.REJECTED)
    await audit.record(
        session,
        run_id=run.id,
        actor=ACTOR,
        event_type="approval.rejected",
        payload=_decision_payload(approval, reviewer_id, note),
    )
    await set_run_status(session, run, RunStatus.COMPLETED, now=now, actor=ACTOR)
    await set_ticket_status(session, ticket, TicketStatus.OPEN, run_id=run.id, actor=ACTOR)
    await session.commit()
    return DecisionResponse(
        approval_id=approval.id,
        proposal_id=proposal.id,
        decision="rejected",
        proposal_status=proposal.status,
        refund=None,
        message=REJECTED_MESSAGE,
    )


async def _expire(
    session: AsyncSession,
    approval: Approval,
    proposal: ActionProposal,
    run: WorkflowRun,
    ticket: Ticket,
    now: datetime,
) -> NoReturn:
    """Too late to decide: record the expiry, reopen the ticket, then refuse."""
    await proposals.record_decision(
        session, approval, decision=ApprovalDecision.EXPIRED, reviewer_id=None, decided_at=now
    )
    await proposals.set_proposal_status(session, proposal, ProposalStatus.EXPIRED)
    await audit.record(
        session,
        run_id=run.id,
        actor="system",
        event_type="approval.expired",
        payload={"approval_id": str(approval.id), "proposal_id": str(proposal.id)},
    )
    await set_run_status(session, run, RunStatus.ESCALATED, now=now, actor="system")
    await set_ticket_status(session, ticket, TicketStatus.OPEN, run_id=run.id, actor="system")
    await session.commit()
    raise AppError(
        409, "APPROVAL_EXPIRED", "This approval has expired. Create a new proposal to continue."
    )


async def _refuse_changed(
    session: AsyncSession,
    proposal: ActionProposal,
    run: WorkflowRun,
    ticket: Ticket,
    now: datetime,
) -> NoReturn:
    """The stored proposal no longer matches its hash: someone changed it. Never approve."""
    await proposals.set_proposal_status(session, proposal, ProposalStatus.INVALIDATED)
    await audit.record(
        session,
        run_id=run.id,
        actor="system",
        event_type="validation.failed",
        payload={"proposal_id": str(proposal.id), "code": "PROPOSAL_CHANGED"},
    )
    await set_run_status(session, run, RunStatus.ESCALATED, now=now, actor="system")
    await set_ticket_status(session, ticket, TicketStatus.ESCALATED, run_id=run.id, actor="system")
    await session.commit()
    raise AppError(
        409, "PROPOSAL_CHANGED", "The proposal no longer matches what was submitted for review."
    )
