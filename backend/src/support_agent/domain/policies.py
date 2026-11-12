"""Pure rules about which policy date applies. No database, no clock.

Policy versions are in force on calendar dates (effective_from / effective_to). An order
is governed by the versions in force on the date it was placed (ADR-005, FR-5). We take
that date in UTC, so the same order always maps to the same date whatever the server's
time zone is.
"""

from datetime import UTC, date, datetime

REFUND_POLICY_KEY = "refund_policy"


def policy_date(moment: datetime) -> date:
    """The calendar date (UTC) used to choose policy versions for a moment in time."""
    if moment.tzinfo is None:
        raise ValueError("policy_date needs a timezone-aware datetime")
    return moment.astimezone(UTC).date()
