"""Refund proposal payloads and their fingerprint (FR-13). Pure: no database, no I/O.

A proposal's payload is hashed once, when it is created. The approval stores the same
hash, and every later step recomputes the hash from the stored payload. If a single
value changed (say the amount), the hashes differ and the approval no longer counts.
"""

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from support_agent.domain.eligibility import EligibilityDecision
from support_agent.domain.enums import ActionType

# An eligible decision means every one of these checks passed (domain/eligibility.py).
ELIGIBLE_CHECKS = (
    "DELIVERED",
    "RETURN_RECEIVED",
    "INSPECTION_PASSED",
    "WITHIN_RETURN_WINDOW",
    "BALANCE_AVAILABLE",
)
REFUND_REASON = "Return received and inspection passed, within the return window"


def canonical_json(payload: dict[str, Any]) -> str:
    """The same data always gives the same text: sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON, as 64 lowercase hex characters."""
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


class RefundProposalPayload(BaseModel):
    """Exactly what a reviewer approves. Stored as JSON and never edited."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal[ActionType.REQUEST_REFUND_APPROVAL] = ActionType.REQUEST_REFUND_APPROVAL
    order_id: UUID
    order_number: str
    customer_id: UUID
    amount_minor: int = Field(gt=0)  # paise
    currency: str
    reason: str
    checks_passed: list[str]

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def build_refund_payload(
    *,
    order_id: UUID,
    order_number: str,
    customer_id: UUID,
    currency: str,
    decision: EligibilityDecision,
) -> RefundProposalPayload:
    """The amount comes only from the eligibility decision, never from a request."""
    if not decision.eligible:
        raise ValueError("cannot build a refund proposal for an ineligible order")
    return RefundProposalPayload(
        order_id=order_id,
        order_number=order_number,
        customer_id=customer_id,
        amount_minor=decision.refundable_amount_minor,
        currency=currency,
        reason=REFUND_REASON,
        checks_passed=list(ELIGIBLE_CHECKS),
    )
