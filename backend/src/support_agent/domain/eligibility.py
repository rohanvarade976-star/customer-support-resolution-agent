"""Deterministic refund eligibility. A pure function: no database, no clock, no I/O.

The LLM never decides this (rule 1). Code computes it from verified facts (FR-11).

The return window is measured in whole elapsed days (a partial day is dropped):
- from `delivered_at`
- to the date the customer requested the return (so slow warehouse inspection never
  counts against them); if there is no return yet, to `now`.
Day 30 is still inside a 30-day window; day 31 is outside.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from support_agent.domain.enums import InspectionStatus, OrderStatus, ReturnStatus
from support_agent.domain.money import refundable_balance


class ReasonCode(StrEnum):
    """Why a refund is not allowed. Declaration order is the order reasons are reported in."""

    NOT_DELIVERED = "NOT_DELIVERED"
    NO_RETURN = "NO_RETURN"
    RETURN_NOT_RECEIVED = "RETURN_NOT_RECEIVED"
    INSPECTION_PENDING = "INSPECTION_PENDING"
    INSPECTION_FAILED = "INSPECTION_FAILED"
    OUTSIDE_RETURN_WINDOW = "OUTSIDE_RETURN_WINDOW"
    ALREADY_REFUNDED = "ALREADY_REFUNDED"


class OrderFacts(BaseModel):
    """Everything the decision needs. The return fields describe the latest active
    (non-cancelled) return and are None when there is none."""

    model_config = ConfigDict(frozen=True)

    order_status: OrderStatus
    delivered_at: datetime | None
    return_status: ReturnStatus | None
    return_requested_at: datetime | None
    return_received_at: datetime | None
    inspection_status: InspectionStatus | None
    paid_amount_minor: int
    succeeded_refunds_minor: int
    in_progress_refunds_minor: int

    @property
    def balance_minor(self) -> int:
        return refundable_balance(
            self.paid_amount_minor, self.succeeded_refunds_minor, self.in_progress_refunds_minor
        )


class EligibilityDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible: bool
    # Non-zero only when eligible, so a blocked order can never leak a usable amount.
    refundable_amount_minor: int
    reasons: list[ReasonCode]


def _return_reasons(facts: OrderFacts) -> list[ReasonCode]:
    if facts.return_status is None:
        return [ReasonCode.NO_RETURN]
    reasons: list[ReasonCode] = []
    if facts.return_status != ReturnStatus.RECEIVED or facts.return_received_at is None:
        reasons.append(ReasonCode.RETURN_NOT_RECEIVED)
    if facts.inspection_status == InspectionStatus.PENDING:
        reasons.append(ReasonCode.INSPECTION_PENDING)
    elif facts.inspection_status == InspectionStatus.FAILED:
        reasons.append(ReasonCode.INSPECTION_FAILED)
    return reasons


def check_eligibility(facts: OrderFacts, now: datetime, window_days: int) -> EligibilityDecision:
    """Return every reason a refund is blocked, or an eligible decision with the amount."""
    reasons: list[ReasonCode] = []

    if facts.order_status != OrderStatus.DELIVERED or facts.delivered_at is None:
        # Nothing return-related is meaningful before delivery.
        reasons.append(ReasonCode.NOT_DELIVERED)
    else:
        reasons.extend(_return_reasons(facts))
        window_end = facts.return_requested_at if facts.return_requested_at is not None else now
        if (window_end - facts.delivered_at).days > window_days:
            reasons.append(ReasonCode.OUTSIDE_RETURN_WINDOW)

    balance = facts.balance_minor
    if balance == 0:
        reasons.append(ReasonCode.ALREADY_REFUNDED)

    eligible = not reasons
    return EligibilityDecision(
        eligible=eligible, refundable_amount_minor=balance if eligible else 0, reasons=reasons
    )
