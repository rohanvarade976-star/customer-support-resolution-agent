"""Request and response models for refund proposals, the approval queue and decisions.

Creating a proposal takes no request body at all: the amount comes only from the
eligibility rules, so there is nowhere for a caller to send one.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from support_agent.domain.enums import ProposalStatus, RefundOperationStatus

MAX_NOTE_LENGTH = 1000


class ProposalResponse(BaseModel):
    proposal_id: UUID
    approval_id: UUID
    run_id: UUID
    ticket_id: UUID
    order_number: str
    amount_minor: int  # paise
    currency: str
    payload_hash: str
    status: ProposalStatus
    expires_at: datetime


class PendingApproval(BaseModel):
    approval_id: UUID
    proposal_id: UUID
    ticket_id: UUID
    order_number: str
    customer_name: str
    amount_minor: int  # paise
    currency: str
    reason: str
    reasons: list[str]  # the eligibility checks that passed
    created_at: datetime
    expires_at: datetime


class ApprovalListResponse(BaseModel):
    items: list[PendingApproval]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)


class RefundSummary(BaseModel):
    refund_id: UUID
    status: RefundOperationStatus
    amount_minor: int  # paise
    provider_reference: str | None


class DecisionResponse(BaseModel):
    approval_id: UUID
    proposal_id: UUID
    decision: Literal["approved", "rejected"]
    proposal_status: ProposalStatus
    refund: RefundSummary | None  # None when rejected
    message: str  # states only what has been verified
