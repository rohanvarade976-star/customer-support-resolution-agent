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
from support_agent.domain.enums import (
    ActionType,
    ApprovalDecision,
    ProposalStatus,
    RunMode,
)


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


async def open_proposal_for_run(
    session: AsyncSession, run_id: UUID, order_id: UUID, now: datetime
) -> ActionProposal | None:
    """This run's own proposal for this order that still awaits a decision, if any.

    Lets the workflow's proposal step run twice (a worker died after the proposal was
    committed but before the graph saved its step) and get the same proposal back.
    """
    stmt = (
        select(ActionProposal)
        .join(Approval, Approval.proposal_id == ActionProposal.id)
        .join(WorkflowRun, WorkflowRun.id == ActionProposal.run_id)
        .join(Ticket, Ticket.id == WorkflowRun.ticket_id)
        .where(
            ActionProposal.run_id == run_id,
            ActionProposal.status == ProposalStatus.PENDING_APPROVAL,
            Approval.decision == ApprovalDecision.PENDING,
            Approval.expires_at > now,
            Ticket.order_id == order_id,
        )
        .limit(1)
    )
    return await session.scalar(stmt)


async def lock_next_expired_workflow_approval(
    session: AsyncSession, now: datetime
) -> Approval | None:
    """One undecided approval past its expiry on a workflow run, locked; or None.

    SKIP LOCKED: an approval a reviewer is deciding right now (the decision endpoint holds
    its lock) is skipped, not waited for. Manual (M3) runs are left out: they keep their
    own path and are expired when a reviewer tries to decide them.
    """
    stmt = (
        select(Approval)
        .join(ActionProposal, ActionProposal.id == Approval.proposal_id)
        .join(WorkflowRun, WorkflowRun.id == ActionProposal.run_id)
        .where(
            Approval.decision == ApprovalDecision.PENDING,
            Approval.expires_at <= now,
            ActionProposal.status == ProposalStatus.PENDING_APPROVAL,
            WorkflowRun.mode != RunMode.MANUAL,
        )
        .order_by(Approval.expires_at, Approval.id)
        .limit(1)
        .with_for_update(of=Approval, skip_locked=True)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


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


@dataclass(frozen=True)
class ApprovalCaseRecord:
    """An approval with everything around it: proposal, run, ticket and customer name."""

    approval: Approval
    proposal: ActionProposal
    run: WorkflowRun
    ticket: Ticket
    customer_name: str


async def get_approval_case(session: AsyncSession, approval_id: UUID) -> ApprovalCaseRecord | None:
    """One approval and its case, in one joined query. Read only: nothing is locked."""
    stmt = (
        select(Approval, ActionProposal, WorkflowRun, Ticket, Customer.name)
        .join(ActionProposal, ActionProposal.id == Approval.proposal_id)
        .join(WorkflowRun, WorkflowRun.id == ActionProposal.run_id)
        .join(Ticket, Ticket.id == WorkflowRun.ticket_id)
        .join(Customer, Customer.id == Ticket.customer_id)
        .where(Approval.id == approval_id)
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(stmt)).first()
    return None if row is None else ApprovalCaseRecord(*row)
