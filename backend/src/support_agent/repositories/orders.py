"""All SQL for orders. Every query is filtered by customer_id, so another customer's
order can never come back from here."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from support_agent.db.models import Order

# selectinload fetches all items (and all returns) in one extra query each, however
# many orders there are: 3 queries in total, never one per order (no N+1).
_LOAD_CHILDREN = (selectinload(Order.items), selectinload(Order.returns))


async def list_for_customer(session: AsyncSession, customer_id: UUID) -> list[Order]:
    stmt = (
        select(Order)
        .where(Order.customer_id == customer_id)
        .options(*_LOAD_CHILDREN)
        .order_by(Order.placed_at.desc(), Order.id)
    )
    return list((await session.scalars(stmt)).all())


async def get_for_customer(
    session: AsyncSession, customer_id: UUID, order_number: str
) -> Order | None:
    stmt = (
        select(Order)
        .where(Order.customer_id == customer_id, Order.order_number == order_number)
        .options(*_LOAD_CHILDREN)
    )
    return await session.scalar(stmt)


async def find_id_for_customer(
    session: AsyncSession, customer_id: UUID, order_number: str
) -> UUID | None:
    """Just the id, for callers that only need to know "does this customer own it"."""
    stmt = select(Order.id).where(
        Order.customer_id == customer_id, Order.order_number == order_number
    )
    return await session.scalar(stmt)


async def lock_by_id(session: AsyncSession, order_id: UUID) -> Order | None:
    """SELECT ... FOR UPDATE on one order row (no children loaded).

    Anyone else reserving a refund on this order waits until the caller commits, so
    two refunds can never both pass the balance check. Keep the transaction short and
    never make a network call while holding it.
    """
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)
