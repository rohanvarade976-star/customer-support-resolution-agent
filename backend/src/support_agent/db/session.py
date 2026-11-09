"""Async database engine and session factory.

Both are created lazily on first use (not at import time), so importing a module
never needs a database or a DATABASE_URL. They are cached, so the whole app shares
one engine and one connection pool.

Usage:
    async with get_sessionmaker()() as session:
        ...
"""

from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from support_agent.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Return the app-wide engine (it owns the connection pool)."""
    url = get_settings().database_url.get_secret_value()
    # pool_pre_ping checks a pooled connection is alive before using it,
    # e.g. after the database restarted or a hosted DB dropped idle connections.
    return create_async_engine(url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the factory that creates one AsyncSession per unit of work."""
    # expire_on_commit=False keeps loaded attributes usable after commit.
    # In async code, touching an expired attribute would trigger hidden I/O and fail.
    return async_sessionmaker(get_engine(), expire_on_commit=False)
