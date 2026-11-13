"""Fixtures for tests that need a real Postgres database.

These tests use TEST_DATABASE_URL, never DATABASE_URL, because they wipe the schema.
Each test runs inside a transaction that is rolled back afterwards, so tests
never see each other's rows.
"""

import asyncio
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from mock_providers.shipping.app import create_app as create_shipping_app
from mock_providers.shipping.config import ShippingSettings
from support_agent.agents.fake import AgentFake
from support_agent.api.deps import (
    get_branch_sessions,
    get_embedder,
    get_llm_client,
    get_session,
    get_shipping_client,
)
from support_agent.core.config import get_settings
from support_agent.core.security import hash_password
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import RetrySettings, RoutedLLMClient
from support_agent.llm.fake import FakeProvider, ScriptStep
from support_agent.main import create_app
from support_agent.rag.documents import load_policy_directory
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.rag.ingest import ingest_policies
from support_agent.workflow.deps import BranchSessions

BACKEND_DIR = Path(__file__).resolve().parents[2]
POLICY_DIR = BACKEND_DIR / "data" / "policies"
# The instant the tests seed the database and run the mock shipping provider at.
SEED_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


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


@pytest.fixture
async def ingested_policies(session: AsyncSession) -> None:
    """The real policy files, ingested with the fake embedder (rolled back after the test).

    Eligibility reads the refund window from the refund policy in force on the order's
    date (ADR-005), so any test that checks eligibility needs these rows.
    """
    await ingest_policies(session, load_policy_directory(POLICY_DIR), FakeEmbedder())


@pytest.fixture
def branch_sessions(session: AsyncSession) -> BranchSessions:
    """Every parallel branch gets the test session, one branch at a time.

    The test session is bound to one connection inside a transaction that is rolled back at
    the end (so the seeded rows are visible, and nothing is left behind). A second session
    could not see those rows, and two tasks cannot share one connection at the same moment,
    so the lock makes the branches take turns. The real concurrency (separate sessions that
    overlap) is tested in tests/unit/test_workflow_multi_agent.py.
    """
    lock = asyncio.Lock()

    @asynccontextmanager
    async def branch() -> AsyncIterator[AsyncSession]:
        async with lock:
            yield session

    return branch


@pytest.fixture
def app(session: AsyncSession, branch_sessions: BranchSessions) -> FastAPI:
    """The real app, using the rolled-back test session instead of the real database.

    The shipping provider is the in-memory mock (no network) and the embedder is the fake.
    """
    application = create_app()

    async def test_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def test_shipping_client() -> AsyncIterator[ShippingClient]:
        mock = create_shipping_app(ShippingSettings(app_env="test"), clock=lambda: SEED_NOW)
        transport = httpx.ASGITransport(app=mock)
        async with ShippingClient(get_settings(), transport=transport) as shipping:
            yield shipping

    application.dependency_overrides[get_session] = test_session
    application.dependency_overrides[get_shipping_client] = test_shipping_client
    application.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    application.dependency_overrides[get_branch_sessions] = lambda: branch_sessions
    return application


@pytest.fixture
def use_llm(app: FastAPI) -> Callable[[list[ScriptStep]], FakeProvider]:
    """use_llm(script): make the app's LLM client a FakeProvider playing `script`."""

    def install(script: list[ScriptStep]) -> FakeProvider:
        fake = FakeProvider(script)
        client = RoutedLLMClient(
            [fake],
            RetrySettings(max_attempts=1, backoff_base_seconds=0, backoff_max_seconds=0),
            default_temperature=0.0,
            default_max_tokens=256,
        )
        app.dependency_overrides[get_llm_client] = lambda: client
        return fake

    return install


@pytest.fixture
def use_agents(app: FastAPI) -> Callable[[dict[str, list[ScriptStep]]], AgentFake]:
    """use_agents(scripts): the app's LLM is an AgentFake with one script per agent.

    Needed once agents run in parallel: they reach the model in no fixed order, so each gets
    its own script (see agents/fake.py).
    """

    def install(scripts: dict[str, list[ScriptStep]]) -> AgentFake:
        fake = AgentFake(scripts)
        client = RoutedLLMClient(
            [fake],
            RetrySettings(max_attempts=1, backoff_base_seconds=0, backoff_max_seconds=0),
            default_temperature=0.0,
            default_max_tokens=256,
        )
        app.dependency_overrides[get_llm_client] = lambda: client
        return fake

    return install


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client that calls the app in-process (no server, no network)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture(scope="session")
def demo_password() -> str:
    """The password of every seeded account in these tests (a test value, not a secret)."""
    return "demo-pass-for-tests"


@pytest.fixture(scope="session")
def demo_password_hash(demo_password: str) -> str:
    # Hashed once per run; Argon2 is slow on purpose.
    return hash_password(demo_password)


@pytest.fixture
def login(
    client: httpx.AsyncClient, demo_password: str
) -> Callable[..., Awaitable[httpx.Response]]:
    """login(email, password=None): POST /auth/login and return the raw response."""

    async def do_login(email: str, password: str | None = None) -> httpx.Response:
        body = {"email": email, "password": demo_password if password is None else password}
        return await client.post("/auth/login", json=body)

    return do_login


@pytest.fixture
def auth_headers(
    login: Callable[..., Awaitable[httpx.Response]],
) -> Callable[[str], Awaitable[dict[str, str]]]:
    """auth_headers(email): log in as a seeded account and return its Authorization header."""

    async def headers_for(email: str) -> dict[str, str]:
        response = await login(email)
        assert response.status_code == 200
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return headers_for
