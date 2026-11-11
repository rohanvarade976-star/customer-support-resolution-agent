"""Support tickets. Who may see what is decided in the service, not here."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from support_agent.api.deps import CustomerDep, PrincipalDep, SessionDep
from support_agent.schemas.tickets import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CreateTicketRequest,
    ReviewerTicketResponse,
    TicketListResponse,
    TicketResponse,
)
from support_agent.services import tickets as tickets_service

router = APIRouter(tags=["tickets"])


@router.post("/tickets", status_code=201)
async def create_ticket(
    body: CreateTicketRequest, principal: CustomerDep, session: SessionDep
) -> TicketResponse:
    return await tickets_service.create_ticket(session, principal, body)


@router.get("/tickets")
async def list_tickets(
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TicketListResponse:
    return await tickets_service.list_tickets(session, principal, limit=limit, offset=offset)


@router.get("/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: UUID, principal: PrincipalDep, session: SessionDep
) -> ReviewerTicketResponse | TicketResponse:
    return await tickets_service.get_ticket(session, principal, ticket_id)
