"""Read-only SQL for the money invariant checker (services/invariants.py, ADR-015).

Every query here only reads, and each one returns ids, statuses and amounts: never names,
emails, messages or any other customer text. The checker deliberately does not reuse the
services' queries: it is an independent second opinion on what the tables hold.

`grace_seconds`: the "stuck" queries ignore rows changed less than that long ago, so work
still in flight on a live system is not reported. The age is measured with the database's
own clock, the same clock that wrote `updated_at`.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, exists, func, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import (
    ActionProposal,
    Approval,
    Job,
    Order,
    RefundOperation,
    Ticket,
    WorkflowRun,
)
from support_agent.domain.enums import (
    ApprovalDecision,
    JobStatus,
    RefundOperationStatus,
    RunStatus,
    TicketStatus,
)
from support_agent.domain.refund_outcomes import UNSETTLED_STATUSES

PROVIDER_TABLE = "mock_payments.refunds"


@dataclass(frozen=True)
class RefundFacts:
    """One refund operation, with the proposal it executes and that proposal's approvals."""

    refund_id: UUID
    proposal_id: UUID
    order_id: UUID
    amount_minor: int
    status: RefundOperationStatus
    idempotency_key: str
    provider_reference: str | None
    proposal_payload: dict[str, Any]
    proposal_payload_hash: str
    approved_hashes: tuple[str, ...]  # payload hashes of this proposal's approved approvals


@dataclass(frozen=True)
class ProviderRefund:
    """One refund as the payment provider stores it."""

    provider_reference: str
    idempotency_key: str
    amount_minor: int
    final_outcome: str  # "succeeded" or "failed": where the refund ends up


@dataclass(frozen=True)
class ResolvedTicketRefund:
    """A resolved ticket whose latest run started a refund, and that run's newest refund."""

    ticket_id: UUID
    refund_id: UUID
    status: RefundOperationStatus
    idempotency_key: str


def _older_than(column: Any, grace_seconds: int) -> Any:
    return column <= func.now() - func.make_interval(0, 0, 0, 0, 0, 0, grace_seconds)


def _latest_run_ids() -> Select[UUID]:
    """Each ticket's most recent run (by start time, then id)."""
    return (
        select(WorkflowRun.id)
        .ext(distinct_on(WorkflowRun.ticket_id))
        .order_by(WorkflowRun.ticket_id, WorkflowRun.started_at.desc(), WorkflowRun.id.desc())
    )


async def over_refunded_orders(session: AsyncSession) -> list[UUID]:
    """Orders whose refunds that did not fail add up to more than was paid."""
    totals = (
        select(
            RefundOperation.order_id,
            func.sum(RefundOperation.amount_minor).label("total"),
        )
        .where(RefundOperation.status != RefundOperationStatus.FAILED)
        .group_by(RefundOperation.order_id)
        .subquery()
    )
    stmt = (
        select(Order.id)
        .join(totals, totals.c.order_id == Order.id)
        .where(totals.c.total > Order.paid_amount_minor)
        .order_by(Order.id)
    )
    return list((await session.scalars(stmt)).all())


async def refund_facts(session: AsyncSession) -> list[RefundFacts]:
    """Every refund operation with its proposal and the hashes of its approved approvals."""
    rows = (
        await session.execute(
            select(RefundOperation, ActionProposal)
            .join(ActionProposal, ActionProposal.id == RefundOperation.proposal_id)
            .order_by(RefundOperation.created_at, RefundOperation.id)
        )
    ).all()
    approved = (
        await session.execute(
            select(Approval.proposal_id, Approval.payload_hash).where(
                Approval.decision == ApprovalDecision.APPROVED
            )
        )
    ).all()
    hashes: dict[UUID, list[str]] = {}
    for proposal_id, digest in approved:
        hashes.setdefault(proposal_id, []).append(digest)
    return [
        RefundFacts(
            refund_id=operation.id,
            proposal_id=proposal.id,
            order_id=operation.order_id,
            amount_minor=operation.amount_minor,
            status=operation.status,
            idempotency_key=operation.idempotency_key,
            provider_reference=operation.provider_reference,
            proposal_payload=proposal.payload,
            proposal_payload_hash=proposal.payload_hash,
            approved_hashes=tuple(hashes.get(proposal.id, ())),
        )
        for operation, proposal in rows
    ]


async def proposals_with_several_refunds(session: AsyncSession) -> list[UUID]:
    stmt = (
        select(RefundOperation.proposal_id)
        .group_by(RefundOperation.proposal_id)
        .having(func.count() > 1)
        .order_by(RefundOperation.proposal_id)
    )
    return list((await session.scalars(stmt)).all())


async def provider_refunds(session: AsyncSession) -> list[ProviderRefund] | None:
    """The payment provider's refunds, or None if its table is not in this database.

    Plain SQL: the provider is a separate service with its own schema, and our app does not
    import its code. Only the columns the checker compares are read.
    """
    present = await session.scalar(text(f"SELECT to_regclass('{PROVIDER_TABLE}') IS NOT NULL"))
    if not present:
        return None
    rows = await session.execute(
        text(
            "SELECT provider_reference, idempotency_key, amount_minor, final_outcome "  # noqa: S608  (constant table name)
            f"FROM {PROVIDER_TABLE} ORDER BY created_at, provider_reference"
        )
    )
    return [
        ProviderRefund(
            provider_reference=row.provider_reference,
            idempotency_key=row.idempotency_key,
            amount_minor=int(row.amount_minor),
            final_outcome=row.final_outcome,
        )
        for row in rows
    ]


async def resolved_ticket_refunds(session: AsyncSession) -> list[ResolvedTicketRefund]:
    """Resolved tickets whose latest run has a refund operation: that run's newest one."""
    stmt = (
        select(
            Ticket.id,
            RefundOperation.id,
            RefundOperation.status,
            RefundOperation.idempotency_key,
        )
        .ext(distinct_on(Ticket.id))
        .join(WorkflowRun, WorkflowRun.ticket_id == Ticket.id)
        .join(ActionProposal, ActionProposal.run_id == WorkflowRun.id)
        .join(RefundOperation, RefundOperation.proposal_id == ActionProposal.id)
        .where(Ticket.status == TicketStatus.RESOLVED, WorkflowRun.id.in_(_latest_run_ids()))
        .order_by(Ticket.id, RefundOperation.created_at.desc(), RefundOperation.id.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [ResolvedTicketRefund(*row) for row in rows]


async def unwatched_refunds(session: AsyncSession, grace_seconds: int) -> list[UUID]:
    """Unsettled refunds nothing will check again, whose run is not with a person."""
    stmt = (
        select(RefundOperation.id)
        .join(ActionProposal, ActionProposal.id == RefundOperation.proposal_id)
        .join(WorkflowRun, WorkflowRun.id == ActionProposal.run_id)
        .where(
            RefundOperation.status.in_(UNSETTLED_STATUSES),
            RefundOperation.next_reconcile_at.is_(None),
            WorkflowRun.status != RunStatus.ESCALATED,
            _older_than(RefundOperation.updated_at, grace_seconds),
        )
        .order_by(RefundOperation.id)
    )
    return list((await session.scalars(stmt)).all())


async def stuck_executing_runs(session: AsyncSession, grace_seconds: int) -> list[UUID]:
    """Executing runs with no scheduled refund check and no open job: nothing will move them."""
    watched_refund = exists(
        select(RefundOperation.id)
        .join(ActionProposal, ActionProposal.id == RefundOperation.proposal_id)
        .where(
            ActionProposal.run_id == WorkflowRun.id,
            RefundOperation.status.in_(UNSETTLED_STATUSES),
            RefundOperation.next_reconcile_at.is_not(None),
        )
    )
    open_job = exists(
        select(Job.id).where(
            Job.run_id == WorkflowRun.id, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])
        )
    )
    stmt = (
        select(WorkflowRun.id)
        .where(
            WorkflowRun.status == RunStatus.EXECUTING,
            _older_than(WorkflowRun.updated_at, grace_seconds),
            ~watched_refund,
            ~open_job,
        )
        .order_by(WorkflowRun.id)
    )
    return list((await session.scalars(stmt)).all())


async def tickets_behind_refunds(session: AsyncSession, grace_seconds: int) -> list[UUID]:
    """Tickets not resolved although their latest run's refund succeeded (run not escalated).

    Only the newest refund of the ticket's latest run counts, so a later, unrelated run on
    the same ticket is not reported.
    """
    newest = (
        select(
            WorkflowRun.ticket_id.label("ticket_id"),
            RefundOperation.status.label("status"),
            RefundOperation.updated_at.label("updated_at"),
        )
        .ext(distinct_on(WorkflowRun.ticket_id))
        .join(ActionProposal, ActionProposal.run_id == WorkflowRun.id)
        .join(RefundOperation, RefundOperation.proposal_id == ActionProposal.id)
        .where(WorkflowRun.id.in_(_latest_run_ids()), WorkflowRun.status != RunStatus.ESCALATED)
        .order_by(
            WorkflowRun.ticket_id, RefundOperation.created_at.desc(), RefundOperation.id.desc()
        )
        .subquery()
    )
    stmt = (
        select(Ticket.id)
        .join(newest, newest.c.ticket_id == Ticket.id)
        .where(
            and_(
                newest.c.status == RefundOperationStatus.SUCCEEDED,
                Ticket.status != TicketStatus.RESOLVED,
                _older_than(newest.c.updated_at, grace_seconds),
            )
        )
        .order_by(Ticket.id)
    )
    return list((await session.scalars(stmt)).all())
