"""Client for the ticketing provider.

Every write sends an Idempotency-Key chosen by the caller, and is retried with that
same key. A retry after a lost response therefore never creates a duplicate ticket or
note: the provider recognises the key and returns the original.

The caller must keep the key stable for one logical action (e.g. derive it from the
run id), so that a later retry of the whole step also reuses it.
"""

from datetime import datetime
from enum import StrEnum
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from support_agent.core.config import Settings
from support_agent.integrations.http import BaseProviderClient, ProviderHttp

PROVIDER = "ticketing"


class ProviderTicketStatus(StrEnum):
    """The ticketing provider's statuses (not our app's TicketStatus)."""

    OPEN = "open"
    PENDING = "pending"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketNote(BaseModel):
    id: str
    ticket_id: str
    body: str
    created_at: datetime


class Ticket(BaseModel):
    id: str
    customer_reference: str
    order_reference: str | None
    subject: str
    description: str
    status: ProviderTicketStatus
    created_at: datetime
    updated_at: datetime
    notes: list[TicketNote]  # oldest first


def _ticket_path(ticket_id: str) -> str:
    return f"/tickets/{quote(ticket_id, safe='')}"


class TicketingClient(BaseProviderClient):
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(ProviderHttp(PROVIDER, settings.ticketing_base_url, settings, transport))

    async def create_ticket(
        self,
        *,
        idempotency_key: str,
        customer_reference: str,
        subject: str,
        description: str,
        order_reference: str | None = None,
    ) -> Ticket:
        body = {
            "customer_reference": customer_reference,
            "order_reference": order_reference,
            "subject": subject,
            "description": description,
        }
        response = await self._http.request(
            "POST", "/tickets", json=body, headers={"Idempotency-Key": idempotency_key}
        )
        return Ticket.model_validate_json(response.content)

    async def get_ticket(self, ticket_id: str) -> Ticket:
        response = await self._http.request("GET", _ticket_path(ticket_id))
        return Ticket.model_validate_json(response.content)

    async def update_status(
        self, ticket_id: str, status: ProviderTicketStatus, *, idempotency_key: str
    ) -> Ticket:
        # Setting a status twice is harmless, but we still send the key like every write.
        response = await self._http.request(
            "PATCH",
            _ticket_path(ticket_id),
            json={"status": status.value},
            headers={"Idempotency-Key": idempotency_key},
        )
        return Ticket.model_validate_json(response.content)

    async def add_note(self, ticket_id: str, body: str, *, idempotency_key: str) -> TicketNote:
        response = await self._http.request(
            "POST",
            f"{_ticket_path(ticket_id)}/notes",
            json={"body": body},
            headers={"Idempotency-Key": idempotency_key},
        )
        return TicketNote.model_validate_json(response.content)
