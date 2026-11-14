"""Request and response models for refund proposals, the approval queue and decisions.

Creating a proposal takes no request body at all: the amount comes only from the
eligibility rules, so there is nowhere for a caller to send one.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from support_agent.domain.eligibility import ReasonCode
from support_agent.domain.enums import (
    ApprovalDecision,
    InspectionStatus,
    OrderStatus,
    ProposalStatus,
    RefundOperationStatus,
    ReturnStatus,
    TicketStatus,
)

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


# ---------------------------------------------------------------- the reviewer's case view
# GET /approvals/{approval_id} (ADR-019). Everything here is stored or computed by code; the
# only model-chosen input is which policy sections were cited, and those are checked against
# our own policy table and shown with our own text.


class CaseProposal(BaseModel):
    """The stored proposal payload, exactly what an approval approves (its hash is checked)."""

    order_number: str
    amount_minor: int  # paise, computed by the eligibility rules when the proposal was made
    currency: str
    reason: str
    checks_passed: list[str]
    payload_hash: str
    status: ProposalStatus


class CaseFacts(BaseModel):
    """The order's facts, read from the database now (not what any agent said)."""

    order_status: OrderStatus
    delivered_at: datetime | None
    return_status: ReturnStatus | None
    return_requested_at: datetime | None
    return_received_at: datetime | None
    inspection_status: InspectionStatus | None
    paid_amount_minor: int
    succeeded_refunds_minor: int
    in_progress_refunds_minor: int
    refundable_balance_minor: int
    refund_window_days: int | None
    # The eligibility rules applied to these facts at this moment. After an approval the
    # refund itself is "in progress", so the order then shows as no longer eligible.
    eligible_now: bool
    reasons_now: list[ReasonCode]


class PolicyCitation(BaseModel):
    citation: str  # chunk key, e.g. "refund_policy_v2:section_3"
    policy_title: str
    version: str
    section_title: str
    text: str  # our own policy text, from policy_chunks


class GoverningPolicy(BaseModel):
    """The refund policy version in force on the order's date (it sets the window)."""

    policy_key: str
    version: str
    title: str
    refund_window_days: int | None


class ApprovalCaseResponse(BaseModel):
    approval_id: UUID
    proposal_id: UUID
    run_id: UUID
    ticket_id: UUID
    approval_status: ApprovalDecision
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    customer_name: str
    complaint: str  # the customer's own words: untrusted, shown as plain text
    ticket_status: TicketStatus
    proposal: CaseProposal
    facts: CaseFacts | None  # None if the order can no longer be read
    governing_policy: GoverningPolicy | None
    citations: list[PolicyCitation]
    refund: RefundSummary | None  # None until a refund operation exists
