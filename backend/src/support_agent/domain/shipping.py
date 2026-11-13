"""Pure shipping-delay facts. No database, no clock, no LLM.

The shipping delays policy says a shipment more than N days past its expected delivery
date, or one the carrier reports as lost, goes to a specialist who can open an
investigation with the carrier. N is not a constant here: it is the policy's
`investigation_threshold_days` (front matter, ADR-005), passed in by the caller.

Days are whole calendar days in UTC: the expected delivery is a date, so "days past" is
today's UTC date minus that date. "More than N" is strictly greater: 7 days late with a
threshold of 7 does not qualify, 8 does. A delivered shipment is never delayed.
"""

from dataclasses import dataclass
from datetime import date, datetime

from support_agent.domain.policies import policy_date

CARRIER_LOST_STATUS = "lost"


@dataclass(frozen=True)
class ShippingDelayFacts:
    """What code has verified about a delay. The resolution agent may only repeat these."""

    delivered: bool
    days_past_expected: int  # 0 when on time or delivered
    carrier_reports_lost: bool
    # None when no shipping policy was found: then no investigation may be promised.
    investigation_threshold_days: int | None
    investigation_applies: bool


def compute_delay_facts(
    *,
    expected_delivery: date,
    delivered_at: datetime | None,
    carrier_status: str,
    now: datetime,
    threshold_days: int | None,
) -> ShippingDelayFacts:
    delivered = delivered_at is not None
    days_late = 0 if delivered else max(0, (policy_date(now) - expected_delivery).days)
    lost = not delivered and carrier_status.strip().lower() == CARRIER_LOST_STATUS
    over_threshold = threshold_days is not None and days_late > threshold_days
    return ShippingDelayFacts(
        delivered=delivered,
        days_past_expected=days_late,
        carrier_reports_lost=lost,
        investigation_threshold_days=threshold_days,
        investigation_applies=threshold_days is not None and (over_threshold or lost),
    )
