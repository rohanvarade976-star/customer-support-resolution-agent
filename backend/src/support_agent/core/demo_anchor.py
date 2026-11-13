"""The one "now" that demo data is built from.

Scenario dates are written as "N days ago" (data/seed/scenarios.json for the database,
data/mock_shipping/shipments.json for the mock shipping provider). Both must count back
from the same instant, or an order says "delivered 2026-11-08" while shipping says
"2026-11-12".

`DEMO_ANCHOR_DATE` (YYYY-MM-DD, optional) pins that instant to 12:00 UTC on that date.
When it is unset, the seed uses the moment it runs and the mock shipping provider uses
the moment it started, so they agree as long as both ran on the same UTC day.

The mock providers never import from `support_agent`, so mock_providers/common/clock.py
holds a copy of this rule; tests/unit/test_demo_anchor.py checks the two agree.
"""

from datetime import UTC, date, datetime, time

ANCHOR_TIME = time(12, 0, tzinfo=UTC)


def resolve_demo_now(anchor_date: date | None, now: datetime) -> datetime:
    """The instant "N days ago" counts back from: the pinned date at noon, else `now`."""
    if anchor_date is None:
        return now
    return datetime.combine(anchor_date, ANCHOR_TIME)
