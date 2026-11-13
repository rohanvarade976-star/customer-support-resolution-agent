"""All SQL for refund operations.

These queries take an order id, not a customer id: callers must already have loaded
the order through a customer-scoped query (repositories/orders.py), which is what
proves ownership. The reconciler's queries take no customer at all: it is system code
working on operations an approved proposal already created.

Reconciliation (ADR-014) works like the job queue (worker/queue.py):

    claim_for_reconciliation -> reconcile_checks + 1, next_reconcile_at = now + lease, commit
    (provider call, no transaction open)
    lock_if_held             -> the row, locked, only if reconcile_checks and status are
                                unchanged since the claim; the caller settles and commits

`reconcile_checks` is the fencing token: a reconciler that lost its lease, or whose
operation the executor settled meanwhile, gets None and writes nothing.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import RefundOperation
from support_agent.domain.enums import RefundOperationStatus
from support_agent.domain.refund_outcomes import UNSETTLED_STATUSES


@dataclass(frozen=True)
class ReconcileClaim:
    """An operation one reconciler holds for one check. `check` is its fencing token."""

    id: UUID
    proposal_id: UUID
    status: RefundOperationStatus
    idempotency_key: str
    provider_reference: str | None
    amount_minor: int
    check: int
    resent: bool


async def sum_by_status(session: AsyncSession, order_id: UUID) -> dict[RefundOperationStatus, int]:
    """Total refund paise per status for one order (statuses with no rows are absent)."""
    stmt = (
        select(RefundOperation.status, func.sum(RefundOperation.amount_minor))
        .where(RefundOperation.order_id == order_id)
        .group_by(RefundOperation.status)
    )
    rows = await session.execute(stmt)
    return {status: int(total) for status, total in rows.all()}  # SUM(bigint) is a Decimal


async def list_for_order(session: AsyncSession, order_id: UUID) -> list[RefundOperation]:
    stmt = (
        select(RefundOperation)
        .where(RefundOperation.order_id == order_id)
        .order_by(RefundOperation.created_at, RefundOperation.id)
    )
    return list((await session.scalars(stmt)).all())


async def get_for_proposal(session: AsyncSession, proposal_id: UUID) -> RefundOperation | None:
    stmt = (
        select(RefundOperation)
        .where(RefundOperation.proposal_id == proposal_id)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def insert_reserved(
    session: AsyncSession,
    *,
    order_id: UUID,
    proposal_id: UUID,
    amount_minor: int,
    idempotency_key: str,
    check_if_stuck_at: datetime,
) -> RefundOperation:
    """`check_if_stuck_at`: when the reconciler takes over if it is still reserved then."""
    operation = RefundOperation(
        order_id=order_id,
        proposal_id=proposal_id,
        amount_minor=amount_minor,
        idempotency_key=idempotency_key,
        status=RefundOperationStatus.RESERVED,
        next_reconcile_at=check_if_stuck_at,
    )
    session.add(operation)
    await session.flush()
    return operation


async def record_first_outcome(
    session: AsyncSession,
    operation: RefundOperation,
    status: RefundOperationStatus,
    provider_reference: str | None,
    *,
    next_check_at: datetime | None,
) -> bool:
    """The executor's write after its provider call: only if the operation is still reserved.

    False: a reconciler settled it meanwhile (the executor stalled past the stale-reserved
    threshold), and its result stands. Either way `operation` is reloaded. Not committed.
    """
    values: dict[str, Any] = {"status": status, "next_reconcile_at": next_check_at}
    if provider_reference is not None:
        values["provider_reference"] = provider_reference
    stmt = (
        update(RefundOperation)
        .where(
            RefundOperation.id == operation.id,
            RefundOperation.status == RefundOperationStatus.RESERVED,
        )
        .values(**values)
        .returning(RefundOperation.id)
        .execution_options(synchronize_session=False)
    )
    written = (await session.execute(stmt)).scalar_one_or_none() is not None
    await session.refresh(operation)
    return written


async def claim_for_reconciliation(
    session: AsyncSession,
    *,
    now: datetime,
    lease_seconds: int,
    skip: Collection[UUID] = (),
) -> ReconcileClaim | None:
    """Take the next operation whose check is due, or None. Commits at once.

    SKIP LOCKED: an operation another reconciler is claiming (or settling) right now is
    skipped, not waited for. Pushing next_reconcile_at to the end of the lease keeps every
    other reconciler away until this check is over, or its reconciler is presumed dead.
    `skip`: operations the caller has already checked in this sweep.
    """
    stmt = (
        select(RefundOperation)
        .where(
            RefundOperation.next_reconcile_at <= now,
            RefundOperation.status.in_(UNSETTLED_STATUSES),
            RefundOperation.id.not_in(skip),
        )
        .order_by(RefundOperation.next_reconcile_at, RefundOperation.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    operation = await session.scalar(stmt)
    if operation is None:
        await session.commit()  # end the (empty) transaction
        return None
    operation.reconcile_checks += 1
    operation.next_reconcile_at = now + timedelta(seconds=lease_seconds)
    claim = ReconcileClaim(
        id=operation.id,
        proposal_id=operation.proposal_id,
        status=operation.status,
        idempotency_key=operation.idempotency_key,
        provider_reference=operation.provider_reference,
        amount_minor=operation.amount_minor,
        check=operation.reconcile_checks,
        resent=operation.resent_at is not None,
    )
    await session.commit()
    return claim


async def lock_if_held(session: AsyncSession, claim: ReconcileClaim) -> RefundOperation | None:
    """Lock the operation for settling, if the claim still holds. Not committed.

    None: another reconciler claimed it since (a higher check count), or the executor
    recorded its own outcome (a different status). Then this reconciler must write nothing.
    """
    stmt = (
        select(RefundOperation)
        .where(
            RefundOperation.id == claim.id,
            RefundOperation.reconcile_checks == claim.check,
            RefundOperation.status == claim.status,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def mark_resent(session: AsyncSession, claim: ReconcileClaim, *, now: datetime) -> bool:
    """Record the one allowed re-send, BEFORE it is sent. Commits.

    Fenced like a settle, and only if it was never re-sent: even two reconcilers, or one
    that crashes mid-send and is followed by another, can re-send at most once in total.
    """
    stmt = (
        update(RefundOperation)
        .where(
            RefundOperation.id == claim.id,
            RefundOperation.reconcile_checks == claim.check,
            RefundOperation.status == claim.status,
            RefundOperation.resent_at.is_(None),
        )
        .values(resent_at=now)
        .returning(RefundOperation.id)
        .execution_options(synchronize_session=False)
    )
    marked = (await session.execute(stmt)).scalar_one_or_none() is not None
    await session.commit()
    return marked
