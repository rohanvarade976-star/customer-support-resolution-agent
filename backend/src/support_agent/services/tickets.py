"""Ticket creation and reads. All ownership checks for tickets live here.

Customers only ever touch their own tickets; reviewers and admins can read all.
Anyone else's ticket, or a ticket that does not exist, is the same 404.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.errors import AppError
from support_agent.domain.enums import Role
from support_agent.repositories import orders, tickets
from support_agent.repositories.tickets import TicketRecord
from support_agent.schemas.auth import Principal
from support_agent.schemas.tickets import (
    CreateTicketRequest,
    ReviewerTicketResponse,
    TicketListResponse,
    TicketResponse,
)
from support_agent.services.orders import order_not_found


def ticket_not_found() -> AppError:
    return AppError(404, "TICKET_NOT_FOUND", "Ticket not found.")


def _is_staff(principal: Principal) -> bool:
    return principal.role in (Role.REVIEWER, Role.ADMIN)


def _customer_view(record: TicketRecord) -> TicketResponse:
    ticket = record.ticket
    return TicketResponse(
        id=ticket.id,
        order_number=record.order_number,
        category=ticket.category,
        status=ticket.status,
        summary=ticket.summary,
        created_at=ticket.created_at,
    )


def _to_response(
    record: TicketRecord, principal: Principal
) -> ReviewerTicketResponse | TicketResponse:
    customer_view = _customer_view(record)
    if not _is_staff(principal):
        return customer_view
    return ReviewerTicketResponse(
        **customer_view.model_dump(),
        customer_id=record.ticket.customer_id,
        customer_name=record.customer_name,
    )


async def create_ticket(
    session: AsyncSession, principal: Principal, body: CreateTicketRequest
) -> TicketResponse:
    if principal.role != Role.CUSTOMER:
        raise AppError(403, "FORBIDDEN", "This action is for customers only.")

    order_id: UUID | None = None
    if body.order_number is not None:
        # Scoped to this customer: someone else's order looks the same as a missing one,
        # and we stop here, before anything is inserted.
        order_id = await orders.find_id_for_customer(session, principal.id, body.order_number)
        if order_id is None:
            raise order_not_found()

    ticket = await tickets.create(
        session, customer_id=principal.id, order_id=order_id, summary=body.message
    )
    await session.commit()
    # Re-read through the same joins as every other read, so the shape is identical.
    record = await tickets.get_by_id(session, ticket.id)
    if record is None:  # cannot happen: we just inserted and committed it
        raise ticket_not_found()
    return _customer_view(record)  # only customers can create, so no reviewer fields


async def list_tickets(
    session: AsyncSession, principal: Principal, *, limit: int, offset: int
) -> TicketListResponse:
    if _is_staff(principal):
        records = await tickets.list_all(session, limit=limit, offset=offset)
    elif principal.role == Role.CUSTOMER:
        records = await tickets.list_for_customer(session, principal.id, limit=limit, offset=offset)
    else:
        records = []
    items: list[ReviewerTicketResponse | TicketResponse] = [
        _to_response(record, principal) for record in records
    ]
    return TicketListResponse(items=items, limit=limit, offset=offset)


async def get_ticket(
    session: AsyncSession, principal: Principal, ticket_id: UUID
) -> ReviewerTicketResponse | TicketResponse:
    record = await tickets.get_by_id(session, ticket_id)
    if record is None:
        raise ticket_not_found()
    owns_it = principal.role == Role.CUSTOMER and record.ticket.customer_id == principal.id
    if not (owns_it or _is_staff(principal)):
        raise ticket_not_found()  # same answer as "no such ticket"
    return _to_response(record, principal)
