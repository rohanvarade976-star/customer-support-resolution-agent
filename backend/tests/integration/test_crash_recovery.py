"""The process dies at each dangerous moment of a refund; the system still ends up right.

Priya's refund runs through the real workflow and the background worker (fake LLM). The
reviewer approves, and the worker's job executing the refund "dies" at one of the executor's
crash points (core/crash_points.py). `SimulatedCrash` is a BaseException, so nothing in the
worker catches it: the job is left `running`, exactly as if the process had been killed,
and the open transaction is rolled back, as a dropped connection would be.

Then nobody helps: the clock moves on, the next worker reclaims the job once its lease has
run out, and the reconciler's sweep settles the refund once it is stale. Each test asserts
the final rows, exactly one refund at the provider, and an empty invariant report.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from mock_providers.payments.app import create_app as create_payments_app
from mock_providers.payments.config import PaymentsSettings
from support_agent.agents.fake import (
    AgentFake,
    critique_reply,
    draft_reply,
    policy_run,
    simple_investigation,
    supervisor_reply,
)
from support_agent.api.deps import get_clock, get_payment_client
from support_agent.core import crash_points
from support_agent.core.config import Settings, get_settings
from support_agent.core.crash_points import CrashPoint, SimulatedCrash
from support_agent.db.models import (
    ActionProposal,
    Approval,
    AuditEvent,
    Customer,
    Job,
    RefundOperation,
    Ticket,
    WorkflowRun,
)
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import (
    JobStatus,
    JobType,
    ProposalStatus,
    RefundOperationStatus,
    RunStatus,
    TicketStatus,
)
from support_agent.integrations.payments import PaymentClient
from support_agent.llm.fake import ScriptStep, tool_reply
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.services import policy_search
from support_agent.services.invariants import check_money_invariants
from support_agent.worker import queue
from support_agent.worker.handlers import WorkerDeps

HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseAgents = Callable[[dict[str, list[ScriptStep]]], AgentFake]
Drain = Callable[[], Awaitable[int]]
BuildDeps = Callable[[], WorkerDeps]

REVIEWER = "anita@example.com"
# The seed date; the mock shipping provider in the app fixture runs at the same instant.
SEED_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


class MutableClock:
    """One clock for the app, the worker, the reconciler and the mock provider."""

    def __init__(self) -> None:
        self.now = SEED_NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Payments:
    app: FastAPI
    engine: AsyncEngine

    def client(self) -> PaymentClient:
        settings = Settings(
            _env_file=None,
            database_url=SecretStr("postgresql+asyncpg://user:pw@localhost:5432/unused"),
            jwt_secret=SecretStr("x" * 32),
            payments_base_url="http://payments.mock",
        )
        return PaymentClient(settings, httpx.ASGITransport(app=self.app))

    async def refund_count(self) -> int:
        async with self.engine.connect() as conn:
            return int(await conn.scalar(text("SELECT count(*) FROM mock_payments.refunds")) or 0)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
async def payments(test_database_url: str, clock: MutableClock) -> AsyncIterator[Payments]:
    settings = PaymentsSettings(app_env="test", database_url=SecretStr(test_database_url))
    app = create_payments_app(settings, clock=clock)
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    async with app.router.lifespan_context(app):
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE mock_payments.refunds"))
        yield Payments(app, engine)
    await engine.dispose()


@pytest.fixture(autouse=True)
def wire_app(app: FastAPI, payments: Payments, clock: MutableClock) -> Settings:
    app.dependency_overrides[get_clock] = lambda: clock

    async def payment_client() -> AsyncIterator[PaymentClient]:
        async with payments.client() as client:
            yield client

    app.dependency_overrides[get_payment_client] = payment_client
    settings = get_settings().model_copy(
        update={
            "policy_min_similarity": -1.0,  # the fake embedder has no meaning
            "job_backoff_base_seconds": 0.0,
            "reconcile_backoff_base_seconds": 0.0,
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str, ingested_policies: None) -> None:
    await seed(session, load_scenarios(), SEED_NOW, demo_password_hash)


@pytest.fixture(autouse=True)
def disarmed() -> Any:
    crash_points.disarm()
    yield
    crash_points.disarm()


# ---------------------------------------------------------------- helpers


async def priya_scripts(session: AsyncSession, settings: Settings) -> dict[str, list[ScriptStep]]:
    priya_id = await session.scalar(
        select(Customer.id).where(Customer.email == "priya@example.com")
    )
    assert priya_id is not None
    found = await policy_search.search_policy(
        session, FakeEmbedder(), settings, priya_id, "return window", "ORD-1001", SEED_NOW
    )
    citation = found.passages[0].citation
    return {
        "classifier": [
            tool_reply("classification", {"category": "refund", "confidence": 0.95, "reason": "t"})
        ],
        "supervisor": [supervisor_reply()],
        "investigator": simple_investigation("ORD-1001"),
        "policy": policy_run(citation),
        "resolution": [draft_reply("refund", citations=[citation])],
        "critic": [critique_reply("approve")],
    }


@dataclass(frozen=True)
class Case:
    ticket_id: UUID
    run_id: UUID
    proposal_id: UUID


async def approved_priya_run(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    drain_jobs: Drain,
) -> Case:
    """Priya's run, paused at approval and then approved: its resume job is queued, not run."""
    priya = await auth_headers("priya@example.com")
    created = await client.post(
        "/tickets",
        json={"message": "I returned it, please refund me", "order_number": "ORD-1001"},
        headers=priya,
    )
    ticket_id = UUID(created.json()["id"])
    started = await client.post(f"/tickets/{ticket_id}/runs", headers=priya)
    assert started.status_code == 202
    run_id = UUID(started.json()["run_id"])
    await drain_jobs()

    proposal = await session.scalar(select(ActionProposal).where(ActionProposal.run_id == run_id))
    assert proposal is not None
    approval = await session.scalar(select(Approval).where(Approval.proposal_id == proposal.id))
    assert approval is not None
    decided = await client.post(
        f"/approvals/{approval.id}/decision",
        json={"decision": "approve"},
        headers=await auth_headers(REVIEWER),
    )
    assert decided.status_code == 202
    return Case(ticket_id, run_id, proposal.id)


async def crash_while_executing(session: AsyncSession, drain_jobs: Drain) -> None:
    """The worker dies inside the refund executor; its connection's open work is lost."""
    with pytest.raises(SimulatedCrash):
        await drain_jobs()
    await session.rollback()


async def reload[M](session: AsyncSession, model: type[M], row_id: UUID) -> M:
    row = await session.get(model, row_id, populate_existing=True)
    assert row is not None
    return row


async def operation_for(session: AsyncSession, case: Case) -> RefundOperation:
    stmt = (
        select(RefundOperation)
        .where(RefundOperation.proposal_id == case.proposal_id)
        .execution_options(populate_existing=True)
    )
    operations = list((await session.scalars(stmt)).all())
    assert len(operations) == 1  # never a second refund for one proposal
    return operations[0]


async def count_events(session: AsyncSession, run_id: UUID, event_type: str) -> int:
    rows = await session.scalars(
        select(AuditEvent.id).where(
            AuditEvent.run_id == run_id, AuditEvent.event_type == event_type
        )
    )
    return len(rows.all())


async def reconcile_via_worker(
    session: AsyncSession, clock: MutableClock, drain_jobs: Drain
) -> None:
    """The worker's scheduled reconciliation sweep, as `run_forever` would queue it."""
    await queue.schedule_sweep(session, JobType.RECONCILE_REFUNDS, now=clock(), max_attempts=3)
    await drain_jobs()


async def recover(
    session: AsyncSession, clock: MutableClock, drain_jobs: Drain, settings: Settings
) -> None:
    """Nobody helps: time passes, a new worker reclaims the job, the reconciler sweeps."""
    clock.advance(settings.job_lease_seconds + 1)
    assert await drain_jobs() == 1  # the dead worker's job, reclaimed and finished
    clock.advance(settings.reconcile_stale_reserved_seconds + 1)
    await reconcile_via_worker(session, clock, drain_jobs)
    clock.advance(30)  # the provider finishes the refund (submitted 2 s, processing 5 s)
    await reconcile_via_worker(session, clock, drain_jobs)


async def assert_refunded_exactly_once(
    session: AsyncSession, payments: Payments, case: Case
) -> RefundOperation:
    operation = await operation_for(session, case)
    assert operation.status == RefundOperationStatus.SUCCEEDED
    assert operation.next_reconcile_at is None
    proposal = await reload(session, ActionProposal, case.proposal_id)
    assert proposal.status == ProposalStatus.EXECUTED
    assert (await reload(session, WorkflowRun, case.run_id)).status == RunStatus.COMPLETED
    assert (await reload(session, Ticket, case.ticket_id)).status == TicketStatus.RESOLVED
    jobs = (await session.scalars(select(Job).where(Job.run_id == case.run_id))).all()
    assert all(job.status == JobStatus.SUCCEEDED for job in jobs)
    assert await payments.refund_count() == 1
    assert await check_money_invariants(session, grace_seconds=0) == []
    return operation


# ---------------------------------------------------------------- the three crash points


async def test_crash_after_reserve_is_sent_once_by_the_reconciler(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    clock: MutableClock,
    drain_jobs: Drain,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    use_agents(await priya_scripts(session, wire_app))
    case = await approved_priya_run(client, session, auth_headers, drain_jobs)

    crash_points.arm(CrashPoint.AFTER_RESERVE)
    await crash_while_executing(session, drain_jobs)
    # The reservation was committed before the crash; nothing reached the provider.
    assert (await operation_for(session, case)).status == RefundOperationStatus.RESERVED
    assert await payments.refund_count() == 0

    await recover(session, clock, drain_jobs, wire_app)

    operation = await assert_refunded_exactly_once(session, payments, case)
    assert operation.resent_at is not None  # the provider had nothing: sent exactly once
    assert await count_events(session, case.run_id, "refund.resent") == 1


async def test_crash_after_provider_call_is_found_by_key_and_never_resent(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    clock: MutableClock,
    drain_jobs: Drain,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    use_agents(await priya_scripts(session, wire_app))
    case = await approved_priya_run(client, session, auth_headers, drain_jobs)

    crash_points.arm(CrashPoint.AFTER_PROVIDER_CALL)
    await crash_while_executing(session, drain_jobs)
    # The provider holds the refund; we only know we reserved it.
    assert (await operation_for(session, case)).status == RefundOperationStatus.RESERVED
    assert await payments.refund_count() == 1

    await recover(session, clock, drain_jobs, wire_app)

    operation = await assert_refunded_exactly_once(session, payments, case)
    assert operation.resent_at is None  # found by its key: nothing was sent again
    assert await count_events(session, case.run_id, "refund.resent") == 0


async def test_crash_after_recording_loses_the_whole_transaction_not_half_of_it(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    clock: MutableClock,
    drain_jobs: Drain,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    use_agents(await priya_scripts(session, wire_app))
    case = await approved_priya_run(client, session, auth_headers, drain_jobs)

    crash_points.arm(CrashPoint.AFTER_RECORD)
    await crash_while_executing(session, drain_jobs)
    # The outcome and the run/ticket updates share one transaction: the crash undid the
    # outcome too, so there is never a recorded refund with a run and ticket that disagree.
    assert (await operation_for(session, case)).status == RefundOperationStatus.RESERVED
    assert await count_events(session, case.run_id, "refund.submitted") == 0
    assert (await reload(session, WorkflowRun, case.run_id)).status == RunStatus.EXECUTING
    assert await payments.refund_count() == 1

    await recover(session, clock, drain_jobs, wire_app)

    operation = await assert_refunded_exactly_once(session, payments, case)
    assert operation.resent_at is None
