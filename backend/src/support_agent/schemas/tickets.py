"""Ticket request and response models."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from support_agent.domain.enums import ReplyAuthor, RunStatus, TicketCategory, TicketStatus

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class CreateTicketRequest(BaseModel):
    # extra="forbid": a body that tries to smuggle in a customer_id is rejected (422).
    # Who the customer is comes only from the token.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    order_number: str | None = Field(default=None, pattern=r"^ORD-[0-9]+$", max_length=20)
    message: str = Field(min_length=1, max_length=2000)


class TicketResponse(BaseModel):
    id: UUID
    order_number: str | None
    category: TicketCategory | None  # None until the classifier has labelled it
    status: TicketStatus
    summary: str
    created_at: datetime
    # The ticket's newest run, so the customer can open its live status. None before a run.
    latest_run_id: UUID | None = None
    latest_run_status: RunStatus | None = None
    # The ticket's verified reply (checked by the output guardrail before it was saved) and who
    # wrote it: the assistant, or a specialist answering an escalated ticket (ADR-022).
    reply: str | None = None
    reply_from: ReplyAuthor = ReplyAuthor.ASSISTANT


class ReviewerTicketResponse(TicketResponse):
    """The same, plus who raised it. Only reviewers and admins ever get this shape."""

    customer_id: UUID
    customer_name: str


class TicketListResponse(BaseModel):
    # Reviewer shape listed first so its extra fields are kept when present.
    items: list[ReviewerTicketResponse | TicketResponse]
    limit: int
    offset: int
