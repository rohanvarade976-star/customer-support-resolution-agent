"""All SQL for long-term customer memory (ADR-013).

Every read and delete names one customer, except the expiry purge, which only deletes.
Times come from the caller's clock, so `created_at` and `expires_at` agree with each other
and tests can move time forward.
"""

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import CustomerMemory, Order
from support_agent.domain.enums import MemoryKind, MemoryOutcome, TicketCategory
from support_agent.domain.memory import MemoryEntry


async def insert_if_absent(
    session: AsyncSession,
    *,
    customer_id: UUID,
    ticket_id: UUID,
    order_id: UUID | None,
    kind: MemoryKind,
    outcome: MemoryOutcome,
    category: TicketCategory | None,
    source_run_id: UUID,
    now: datetime,
    expires_at: datetime,
) -> bool:
    """Add one item unless this run already has one. True if a row was inserted.

    A retried job (at-least-once delivery) reaches here twice; the unique source_run_id
    makes the second insert do nothing.
    """
    stmt = (
        insert(CustomerMemory)
        .values(
            customer_id=customer_id,
            ticket_id=ticket_id,
            order_id=order_id,
            kind=kind,
            outcome_code=outcome,
            category=category,
            source_run_id=source_run_id,
            created_at=now,
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=[CustomerMemory.source_run_id])
        .returning(CustomerMemory.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def set_refund_outcome(
    session: AsyncSession,
    *,
    customer_id: UUID,
    ticket_id: UUID,
    order_id: UUID,
    outcome: MemoryOutcome,
    category: TicketCategory | None,
    source_run_id: UUID,
    now: datetime,
    expires_at: datetime,
) -> None:
    """Make this run's item say how its refund ended, inserting it if the run has none.

    Used by reconciliation: a run that ended with "refund submitted" (or escalated) already
    has an item, and the provider's later, final answer replaces what it says.
    """
    stmt = insert(CustomerMemory).values(
        customer_id=customer_id,
        ticket_id=ticket_id,
        order_id=order_id,
        kind=MemoryKind.REFUND_EXECUTED,
        outcome_code=outcome,
        category=category,
        source_run_id=source_run_id,
        created_at=now,
        expires_at=expires_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[CustomerMemory.source_run_id],
        set_={"kind": stmt.excluded.kind, "outcome_code": stmt.excluded.outcome_code},
    )
    await session.execute(stmt)


async def list_recent(
    session: AsyncSession, customer_id: UUID, *, now: datetime, limit: int
) -> list[MemoryEntry]:
    """This customer's unexpired items, newest first. Expired rows are never returned,
    even before the purge job has deleted them."""
    stmt = (
        select(
            CustomerMemory.kind,
            CustomerMemory.outcome_code,
            CustomerMemory.category,
            Order.order_number,
            CustomerMemory.created_at,
        )
        .outerjoin(Order, Order.id == CustomerMemory.order_id)
        .where(CustomerMemory.customer_id == customer_id, CustomerMemory.expires_at > now)
        .order_by(CustomerMemory.created_at.desc(), CustomerMemory.id)
        .limit(limit)
    )
    rows = await session.execute(stmt)
    return [
        MemoryEntry(
            kind=row.kind,
            outcome=row.outcome_code,
            category=row.category,
            order_number=row.order_number,
            recorded_on=row.created_at.date(),
        )
        for row in rows
    ]


async def list_for_customer(
    session: AsyncSession, customer_id: UUID, *, now: datetime
) -> list[tuple[CustomerMemory, str | None]]:
    """Every unexpired item of this customer with its order number, newest first (the API)."""
    stmt = (
        select(CustomerMemory, Order.order_number)
        .outerjoin(Order, Order.id == CustomerMemory.order_id)
        .where(CustomerMemory.customer_id == customer_id, CustomerMemory.expires_at > now)
        .order_by(CustomerMemory.created_at.desc(), CustomerMemory.id)
    )
    return [(row[0], row[1]) for row in await session.execute(stmt)]


async def delete_for_customer(session: AsyncSession, customer_id: UUID) -> int:
    """Delete all of this customer's items, expired or not. Returns how many."""
    result = await session.execute(
        delete(CustomerMemory).where(CustomerMemory.customer_id == customer_id)
    )
    return _rowcount(result)


async def delete_expired(session: AsyncSession, *, now: datetime) -> int:
    """Delete every item whose expiry has passed, for all customers. Returns how many."""
    result = await session.execute(delete(CustomerMemory).where(CustomerMemory.expires_at <= now))
    return _rowcount(result)


def _rowcount(result: Any) -> int:
    # A DELETE always returns a CursorResult; the type stubs only say Result.
    return cast(CursorResult[Any], result).rowcount
