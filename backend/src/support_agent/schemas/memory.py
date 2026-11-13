"""Responses for the customer's own long-term memory (GET and DELETE /me/memory)."""

from datetime import datetime

from pydantic import BaseModel

from support_agent.domain.enums import MemoryKind, MemoryOutcome, TicketCategory


class MemoryItemResponse(BaseModel):
    """One remembered past case. Fixed codes only: there is no free text to show."""

    kind: MemoryKind
    outcome_code: MemoryOutcome
    category: TicketCategory | None
    order_number: str | None
    created_at: datetime
    expires_at: datetime


class MemoryDeletedResponse(BaseModel):
    deleted: int  # how many items were removed
