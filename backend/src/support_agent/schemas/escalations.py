"""The reviewer's escalations inbox and its actions (ADR-022). Reviewers only."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from support_agent.domain.enums import ReplyAuthor, TicketCategory, TicketStatus
from support_agent.domain.escalations import EscalationCode
from support_agent.schemas.approvals import CaseFacts, GoverningPolicy

MAX_REPLY_LENGTH = 2000  # the same limit as a customer's message
MAX_RESOLVE_NOTE_LENGTH = 1000  # the same limit as an approval note


class EscalationReasonView(BaseModel):
    code: EscalationCode
    codes: list[str] = []  # detail codes, e.g. the workflow error or risk flag codes


class EscalationItem(BaseModel):
    ticket_id: UUID
    run_id: UUID  # the escalated run: its timeline and AI analysis
    customer_name: str
    order_number: str | None
    category: TicketCategory | None
    complaint: str  # the customer's own words: untrusted, shown as plain text
    escalated_at: datetime
    reasons: list[EscalationReasonView]  # computed by code from the audit trail
    specialist_replied: bool


class EscalationListResponse(BaseModel):
    items: list[EscalationItem]  # newest escalation first


class EscalationCase(EscalationItem):
    ticket_status: TicketStatus
    reply: str | None  # what the customer sees now
    reply_from: ReplyAuthor
    facts: CaseFacts | None  # None when the ticket names no order or it cannot be read
    currency: str | None  # the order's currency, for showing the facts' amounts
    governing_policy: GoverningPolicy | None


class SpecialistReplyRequest(BaseModel):
    # Only the text: who sends it comes from the token, which ticket from the URL.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=MAX_REPLY_LENGTH)


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Kept in the audit trail only; never shown to the customer and never streamed.
    note: str = Field(min_length=1, max_length=MAX_RESOLVE_NOTE_LENGTH)


class EscalationActionResponse(BaseModel):
    ticket_id: UUID
    ticket_status: TicketStatus
    reply_from: ReplyAuthor
