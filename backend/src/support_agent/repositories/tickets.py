"""All SQL for tickets.

Reads return a `TicketRecord`: the ticket plus the customer's name and the order
number, fetched in the same query by joins (no extra lookups per ticket).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import Customer, Order, Ticket
from support_agent.domain.enums import TicketCategory, TicketStatus


@dataclass(frozen=True)
class TicketRecord:
    ticket: Ticket
    customer_name: str
    order_number: str | None  # None when the ticket does not name an order


def _select_records() -> Select[Ticket, str, str]:  # order_number is really str | None
    return (
        select(Ticket, Customer.name, Order.order_number)
        .join(Customer, Customer.id == Ticket.customer_id)
        .outerjoin(Order, Order.id == Ticket.order_id)  # outer: order_id may be NULL
    )


def _newest_first[*Ts](stmt: Select[*Ts]) -> Select[*Ts]:
    return stmt.order_by(Ticket.created_at.desc(), Ticket.id)  # id breaks ties stably


async def _fetch(session: AsyncSession, stmt: Select[Ticket, str, str]) -> list[TicketRecord]:
    rows = (await session.execute(stmt)).all()
    return [TicketRecord(ticket, name, number) for ticket, name, number in rows]


async def create(
    session: AsyncSession, *, customer_id: UUID, order_id: UUID | None, summary: str
) -> Ticket:
    # category stays NULL (the classifier sets it later); status uses the model default.
    ticket = Ticket(customer_id=customer_id, order_id=order_id, summary=summary)
    session.add(ticket)
    await session.flush()
    return ticket


async def get_by_id(session: AsyncSession, ticket_id: UUID) -> TicketRecord | None:
    records = await _fetch(session, _select_records().where(Ticket.id == ticket_id))
    return records[0] if records else None


async def list_for_customer(
    session: AsyncSession, customer_id: UUID, *, limit: int, offset: int
) -> list[TicketRecord]:
    stmt = _newest_first(_select_records().where(Ticket.customer_id == customer_id))
    return await _fetch(session, stmt.limit(limit).offset(offset))


async def list_all(session: AsyncSession, *, limit: int, offset: int) -> list[TicketRecord]:
    stmt = _newest_first(_select_records())
    return await _fetch(session, stmt.limit(limit).offset(offset))


async def set_category(session: AsyncSession, ticket: Ticket, category: TicketCategory) -> None:
    ticket.category = category
    await session.flush()


async def set_status(session: AsyncSession, ticket: Ticket, status: TicketStatus) -> None:
    ticket.status = status
    await session.flush()
