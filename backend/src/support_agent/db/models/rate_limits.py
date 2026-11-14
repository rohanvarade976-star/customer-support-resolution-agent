from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base


class RateLimitCounter(Base):
    """How many times one client did one thing in one fixed time window (ADR-017).

    The key is a keyed hash of an email, an IP address or a customer id, never the value
    itself. Rows live in Postgres so every API process sees the same counts. A row is
    deleted once its window is over (`expires_at`).
    """

    __tablename__ = "rate_limit_counters"
    __table_args__ = (
        CheckConstraint("count >= 0", name="count_non_negative"),
        Index("ix_rate_limit_counters_expires_at", "expires_at"),
    )

    bucket_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime]
