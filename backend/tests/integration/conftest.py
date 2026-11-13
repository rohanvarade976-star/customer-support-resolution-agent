"""Fixtures for tests that need a real Postgres database.

These tests use TEST_DATABASE_URL, never DATABASE_URL, because they wipe the schema.
Each test runs inside a transaction that is rolled back afterwards, so tests
never see each other's rows. The code under test may still commit and roll back: the test
session turns those into savepoints inside that outer transaction.

The API only queues work since M7 Task 2. `drain_jobs()` plays the background worker: it runs
every due job with the real handlers, on the test session, using the app's dependencies (so
the fake LLM, mock providers and test clock that a test installs apply to the worker too).

The event loop: the checkpointer's driver (psycopg) cannot run on Windows' default Proactor
loop, so these tests run on a selector loop, as the app does (core/event_loop.py). On Linux
that is already the default, so CI is unaffected.
"""

import asyncio
import inspect
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pytest_asyncio.plugin import LoopFactory
from sqlalchemy import delete, make_url, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mock_providers.payments.testing import InMemoryPayments, in_memory_payments
from mock_providers.shipping.app import create_app as create_shipping_app
from mock_providers.shipping.config import ShippingSettings
from support_agent.agents.fake import AgentFake
from support_agent.api.deps import (
    get_branch_sessions,
    get_checkpointer,
    get_clock,
    get_embedder,
    get_llm_client,
    get_payment_client,
    get_session,
    get_shipping_client,
)
from support_agent.core.config import Settings, get_settings
from support_agent.core.security import hash_password
from support_agent.db.models import AuditEvent, Customer, CustomerMemory, Job, Ticket, WorkflowRun
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import RetrySettings, RoutedLLMClient
from support_agent.llm.disabled import DisabledLLMClient
from support_agent.llm.fake import FakeProvider, ScriptStep
from support_agent.main import create_app
from support_agent.rag.documents import load_policy_directory
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.rag.ingest import ingest_policies
from support_agent.services.runs import WorkflowEnv
from support_agent.tools.read_tools import build_read_registry
from support_agent.worker import runner
from support_agent.worker.handlers import WorkerDeps
from support_agent.workflow.checkpointer import open_checkpointer
from support_agent.workflow.deps import BranchSessions
from support_agent.workflow.graph import Checkpointer

BACKEND_DIR = Path(__file__).resolve().parents[2]
POLICY_DIR = BACKEND_DIR / "data" / "policies"
# The instant the tests seed the database and run the mock shipping provider at.
SEED_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, LoopFactory]:
    return {"selector": asyncio.SelectorEventLoop}


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
        # create_savepoint: the code's commit() and rollback() act on savepoints, so a
        # rollback (a failed job) undoes only its own work, never the test's seed rows.
        db = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
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
async def checkpointer(migrated_database: str) -> AsyncIterator[Checkpointer]:
    """The real Postgres checkpointer on the test database, set up as at app startup.

    It uses its own psycopg connections, so its rows are committed (not rolled back with
    the test session). Every run has a fresh thread id, so tests never see each other's.
    """
    async with open_checkpointer(migrated_database, build_read_registry()) as saver:
        yield saver


@pytest.fixture
async def payments(test_database_url: str) -> AsyncIterator[InMemoryPayments]:
    """The mock payment provider in memory, with no refunds (tests wire it into the app)."""
    async with in_memory_payments(test_database_url) as provider:
        yield provider


@pytest.fixture
def app(
    session: AsyncSession, branch_sessions: BranchSessions, checkpointer: Checkpointer
) -> FastAPI:
    """The real app, using the rolled-back test session instead of the real database.

    The shipping provider is the in-memory mock (no network), the embedder is the fake, and
    the checkpointer is the real one on the test database.
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
    application.dependency_overrides[get_checkpointer] = lambda: checkpointer
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


# ---------------------------------------------------------------- the worker, in tests


def _dependency(
    app: FastAPI, dependency: Callable[..., Any], settings: Settings | None = None
) -> Any:
    """Call an app dependency the way FastAPI would, honouring the test's override.

    Overrides take no arguments; the real ones here take at most the settings.
    """
    fn = app.dependency_overrides.get(dependency, dependency)
    return fn(settings) if inspect.signature(fn).parameters else fn()


def _client(
    app: FastAPI, dependency: Callable[..., Any], settings: Settings
) -> AbstractAsyncContextManager[Any]:
    """A provider-client dependency (an async generator) as an async context manager."""
    fn = app.dependency_overrides.get(dependency, dependency)
    factory = asynccontextmanager(fn)
    return factory(settings) if inspect.signature(fn).parameters else factory()


@pytest.fixture
def worker_deps(app: FastAPI, session: AsyncSession) -> Callable[[], WorkerDeps]:
    """worker_deps(): what the worker gets in production, built from the app's dependencies.

    Built on each call, so overrides a test installs later (the fake LLM, a moved clock,
    the mock payment provider) are used. Every session is the test session.
    """

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session

    def build() -> WorkerDeps:
        settings: Settings = _dependency(app, get_settings)
        clock = _dependency(app, get_clock, settings)
        checkpointer: Checkpointer = _dependency(app, get_checkpointer, settings)

        @asynccontextmanager
        async def workflow_env(*, llm_enabled: bool) -> AsyncIterator[WorkflowEnv]:
            async with (
                _client(app, get_payment_client, settings) as payments,
                _client(app, get_shipping_client, settings) as shipping,
            ):
                llm = _dependency(app, get_llm_client, settings) if llm_enabled else None
                yield WorkflowEnv(
                    settings=settings,
                    llm=llm or DisabledLLMClient(),
                    shipping_client=shipping,
                    payments=payments,
                    embedder=_dependency(app, get_embedder, settings),
                    clock=clock,
                    branch_sessions=_dependency(app, get_branch_sessions, settings),
                    checkpointer=checkpointer,
                )

        return WorkerDeps(
            settings=settings,
            clock=clock,
            checkpointer=checkpointer,
            sessions=sessions,
            workflow_env=workflow_env,
        )

    return build


@pytest.fixture
def drain_jobs(worker_deps: Callable[[], WorkerDeps]) -> Callable[[], Awaitable[int]]:
    """drain_jobs(): run every job that is due now, as the worker would. Returns how many."""

    async def drain() -> int:
        return await runner.drain(worker_deps())

    return drain


# ---------------------------------------------------------------- committed rows, two connections


@pytest.fixture
async def committed_sessions(
    migrated_database: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions whose commits are real, each on its own connection.

    The usual `session` fixture lives inside one rolled-back transaction, which another
    connection cannot see. SKIP LOCKED and the one-unfinished-run index only show their
    effect between separate connections, so those tests use these (with `committed_ticket`).
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def committed_ticket(
    committed_sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[UUID, UUID]]:
    """(customer_id, ticket_id), committed; deleted with everything hung on them afterwards."""
    marker = uuid4().hex[:12]
    async with committed_sessions() as db:
        customer = Customer(
            external_auth_subject=f"committed-{marker}",
            name="Committed Test",
            email=f"committed-{marker}@example.com",
        )
        db.add(customer)
        await db.flush()
        ticket = Ticket(customer_id=customer.id, summary="committed test ticket")
        db.add(ticket)
        await db.commit()
        customer_id, ticket_id = customer.id, ticket.id
    try:
        yield customer_id, ticket_id
    finally:
        async with committed_sessions() as db:
            runs = select(WorkflowRun.id).where(WorkflowRun.ticket_id == ticket_id)
            await db.execute(delete(Job).where(Job.run_id.in_(runs)))
            await db.execute(delete(AuditEvent).where(AuditEvent.run_id.in_(runs)))
            await db.execute(delete(CustomerMemory).where(CustomerMemory.ticket_id == ticket_id))
            await db.execute(delete(WorkflowRun).where(WorkflowRun.ticket_id == ticket_id))
            await db.execute(delete(Ticket).where(Ticket.id == ticket_id))
            await db.execute(delete(Customer).where(Customer.id == customer_id))
            await db.commit()
