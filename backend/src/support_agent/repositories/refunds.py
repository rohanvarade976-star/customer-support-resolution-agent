"""All SQL for refund operations.

These queries take an order id, not a customer id: callers must already have loaded
the order through a customer-scoped query (repositories/orders.py), which is what
proves ownership.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import RefundOperation
from support_agent.domain.enums import RefundOperationStatus


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
) -> RefundOperation:
    operation = RefundOperation(
        order_id=order_id,
        proposal_id=proposal_id,
        amount_minor=amount_minor,
        idempotency_key=idempotency_key,
        status=RefundOperationStatus.RESERVED,
    )
    session.add(operation)
    await session.flush()
    return operation


async def set_outcome(
    session: AsyncSession,
    operation: RefundOperation,
    status: RefundOperationStatus,
    provider_reference: str | None,
) -> None:
    operation.status = status
    if provider_reference is not None:
        operation.provider_reference = provider_reference
    await session.flush()
