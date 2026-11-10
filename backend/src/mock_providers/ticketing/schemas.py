"""Request and response models for the mock ticketing provider's HTTP API."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TicketStatus(StrEnum):
    OPEN = "open"
    PENDING = "pending"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_reference: str = Field(min_length=1, max_length=100)
    order_reference: str | None = Field(default=None, min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)


class TicketUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Any status can move to any other, like most ticketing tools (closed tickets can reopen).
    status: TicketStatus


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=5000)


class NoteResponse(BaseModel):
    id: str
    ticket_id: str
    body: str
    created_at: datetime


class TicketResponse(BaseModel):
    id: str
    customer_reference: str
    order_reference: str | None
    subject: str
    description: str
    status: TicketStatus
    created_at: datetime
    updated_at: datetime
    notes: list[NoteResponse]  # oldest first
