"""All SQL for action proposals and their approvals.

A proposal's payload and hash are written once and never updated; only its status
moves. Approvals carry the same hash (the database enforces that with a foreign key).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import (
    ActionProposal,
    Approval,
    Customer,
    Order,
    Ticket,
    WorkflowRun,
)
from support_agent.domain.enums import ActionType, ApprovalDecision, ProposalStatus


@dataclass(frozen=True)
class PendingApprovalRecord:
    approval: Approval
    proposal: ActionProposal
    ticket_id: UUID
    order_number: str
    customer_name: str


async def create_proposal(
    session: AsyncSession, *, run_id: UUID, payload: dict[str, Any], digest: str
) -> ActionProposal:
    proposal = ActionProposal(
        run_id=run_id,
        action_type=ActionType.REQUEST_REFUND_APPROVAL,
        payload=payload,
        payload_hash=digest,
    )
    session.add(proposal)
    await session.flush()
    return proposal


async def create_approval(
    session: AsyncSession,
    *,
    proposal_id: UUID,
    digest: str,
    created_at: datetime,
    expires_at: datetime,
) -> Approval:
    approval = Approval(
        proposal_id=proposal_id,
        payload_hash=digest,
        created_at=created_at,
        expires_at=expires_at,
    )
    session.add(approval)
    await session.flush()
    return approval


async def get_proposal(session: AsyncSession, proposal_id: UUID) -> ActionProposal | None:
    # populate_existing: always re-read the row, never trust a copy already in memory.
    stmt = (
        select(ActionProposal)
        .where(ActionProposal.id == proposal_id)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def lock_approval(session: AsyncSession, approval_id: UUID) -> Approval | None:
    """SELECT ... FOR UPDATE: a second reviewer deciding at the same moment waits here,
    then sees the first decision. Held only until the caller commits."""
    stmt = (
        select(Approval)
        .where(Approval.id == approval_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def get_approval_for_proposal(session: AsyncSession, proposal_id: UUID) -> Approval | None:
    stmt = (
        select(Approval)
        .where(Approval.proposal_id == proposal_id)
        .order_by(Approval.created_at.desc(), Approval.id)
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def order_has_open_proposal(session: AsyncSession, order_id: UUID, now: datetime) -> bool:
    """Is a proposal for this order still waiting for a decision (and not expired)?"""
    stmt = select(
        exists()
        .where(ActionProposal.status == ProposalStatus.PENDING_APPROVAL)
        .where(Approval.proposal_id == ActionProposal.id)
        .where(Approval.decision == ApprovalDecision.PENDING)
        .where(Approval.expires_at > now)
        .where(WorkflowRun.id == ActionProposal.run_id)
        .where(Ticket.id == WorkflowRun.ticket_id)
        .where(Ticket.order_id == order_id)
    )
    return bool(await session.scalar(stmt))


async def list_pending(session: AsyncSession, now: datetime) -> list[PendingApprovalRecord]:
    """Undecided, unexpired approvals, soonest expiry first, in one joined query."""
    stmt = (
        select(Approval, ActionProposal, Ticket.id, Order.order_number, Customer.name)
        .join(ActionProposal, ActionProposal.id == Approval.proposal_id)
        .join(WorkflowRun, WorkflowRun.id == ActionProposal.run_id)
        .join(Ticket, Ticket.id == WorkflowRun.ticket_id)
        .join(Order, Order.id == Ticket.order_id)
        .join(Customer, Customer.id == Ticket.customer_id)
        .where(
            Approval.decision == ApprovalDecision.PENDING,
            Approval.expires_at > now,
            ActionProposal.status == ProposalStatus.PENDING_APPROVAL,
        )
        .order_by(Approval.expires_at, Approval.id)
    )
    rows = (await session.execute(stmt)).all()
    return [PendingApprovalRecord(*row) for row in rows]


async def set_proposal_status(
    session: AsyncSession, proposal: ActionProposal, status: ProposalStatus
) -> None:
    proposal.status = status
    await session.flush()


async def record_decision(
    session: AsyncSession,
    approval: Approval,
    *,
    decision: ApprovalDecision,
    reviewer_id: str | None,
    decided_at: datetime,
) -> None:
    approval.decision = decision
    approval.reviewer_id = reviewer_id
    approval.decided_at = decided_at
    await session.flush()
