"""All SQL for the rate-limit counters (ADR-017)."""

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import RateLimitCounter


async def increment(
    session: AsyncSession, *, bucket_key: str, window_start: datetime, expires_at: datetime
) -> int:
    """Count one more hit in this window and return the new count.

    One statement, so two API processes hitting the same key at once both get their own
    number: the row lock serialises them and neither increment is lost.
    """
    first_hit = insert(RateLimitCounter).values(
        bucket_key=bucket_key, window_start=window_start, count=1, expires_at=expires_at
    )
    stmt = first_hit.on_conflict_do_update(
        index_elements=[RateLimitCounter.bucket_key, RateLimitCounter.window_start],
        set_={"count": RateLimitCounter.count + 1},
    ).returning(RateLimitCounter.count)
    return int((await session.execute(stmt)).scalar_one())


async def delete_expired(session: AsyncSession, now: datetime) -> None:
    """Remove counters whose window is over (an index on `expires_at` serves this)."""
    await session.execute(delete(RateLimitCounter).where(RateLimitCounter.expires_at < now))
