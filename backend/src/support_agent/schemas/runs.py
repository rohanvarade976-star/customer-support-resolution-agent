"""Workflow run response model."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from support_agent.domain.enums import RunStatus, TicketCategory


class RunResponse(BaseModel):
    run_id: UUID
    ticket_id: UUID
    status: RunStatus
    category: TicketCategory | None  # the ticket's category; None until classified
    started_at: datetime
    completed_at: datetime | None
