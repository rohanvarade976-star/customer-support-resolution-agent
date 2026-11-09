"""Fixtures for tests that need a real Postgres database.

These tests use TEST_DATABASE_URL, never DATABASE_URL, because they wipe the schema.
Each test runs inside a transaction that is rolled back afterwards, so tests
never see each other's rows.
"""

import asyncio
import os
import re
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from support_agent.core.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _in_thread[T](fn: Callable[..., T], *args: object) -> T:
    # Alembic's env.py calls asyncio.run(). Running it in a fresh thread keeps it
    # away from the event loop pytest-asyncio manages in the main thread.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args).result()


async def reset_schema(url: str) -> None:
    """Drop every table, index and extension, leaving an empty database."""
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


async def _create_database_if_missing(url: str) -> None:
    target = make_url(url)
    name = target.database or ""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise ValueError("test database name must be a simple lowercase identifier")
    # CREATE DATABASE cannot run inside a transaction, hence AUTOCOMMIT.
    admin = create_async_engine(
        target.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))  # name validated above
    await admin.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    settings = get_settings()
    if settings.test_database_url is None:
        if os.environ.get("CI"):  # GitHub Actions sets CI=true; never skip silently there
            pytest.fail("TEST_DATABASE_URL must be set in CI")
        pytest.skip("TEST_DATABASE_URL is not set; skipping database tests")
    url = settings.test_database_url.get_secret_value()
    if url == settings.database_url.get_secret_value():
        pytest.fail("TEST_DATABASE_URL must differ from DATABASE_URL (tests wipe it)")
    _in_thread(asyncio.run, _create_database_if_missing(url))
    return url


@pytest.fixture(scope="session")
def alembic_cfg(test_database_url: str) -> Config:
    cfg = Config(BACKEND_DIR / "alembic.ini")
    cfg.attributes["database_url"] = test_database_url  # read by migrations/env.py
    return cfg


@pytest.fixture(scope="session")
def migrated_database(test_database_url: str, alembic_cfg: Config) -> str:
    """A freshly wiped test database with all migrations applied (once per test run)."""
    _in_thread(asyncio.run, reset_schema(test_database_url))
    _in_thread(command.upgrade, alembic_cfg, "head")
    return test_database_url


@pytest.fixture
async def empty_database(test_database_url: str) -> str:
    """The test database with nothing in it: no tables, no extensions."""
    await reset_schema(test_database_url)
    return test_database_url


@pytest.fixture
async def session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """A session whose changes are all rolled back when the test ends."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with engine.connect() as conn:
        outer = await conn.begin()
        db = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield db
        finally:
            await db.close()
            await outer.rollback()
    await engine.dispose()
