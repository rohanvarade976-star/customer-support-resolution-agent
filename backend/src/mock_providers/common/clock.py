"""The current time, passed into each app so tests can use a fake clock instead of sleeping."""

from collections.abc import Callable
from datetime import UTC, date, datetime, time

Clock = Callable[[], datetime]

# Same rule as support_agent/core/demo_anchor.py (the mocks cannot import from it).
ANCHOR_TIME = time(12, 0, tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def anchored_clock(anchor_date: date | None, started_at: datetime) -> Clock:
    """A clock frozen at the demo anchor, so scenario dates match the database seed.

    With a pinned `anchor_date` it is noon UTC on that date; without one it is the
    moment the provider started. Restart the provider to move it forward.
    """
    frozen = started_at if anchor_date is None else datetime.combine(anchor_date, ANCHOR_TIME)
    return lambda: frozen
