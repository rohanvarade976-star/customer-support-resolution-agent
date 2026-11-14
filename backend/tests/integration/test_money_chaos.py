"""Seeded chaos: random sequences of money events under random provider faults (ADR-015).

Each sequence opens refund cases (manual and workflow runs) on five eligible orders, then
randomly approves, rejects or lets approvals expire, moves the clock, runs the worker (with
its expiry and reconciliation sweeps) and the reconciler, and sometimes makes the process
"die" at a refund crash point. Every request to the payment provider may meet a fault:
a connect error (nothing sent), a 500 (nothing done), a reply lost after the work was done
(a timeout or a 504), a slow reply, or a declined refund.

Afterwards the faults stop, time passes and the worker and reconciler finish what they can.
Then the invariant checker must report nothing, and the provider must hold no refund we did
not ask for and no more money per order than was paid.

Replaying a failure: the seed is in the test id (`test_money_chaos[seed=1003]`) and in the
failure message, with the steps taken. Run one seed with
    $env:CHAOS_SEED = "1003"; uv run pytest tests/integration/test_money_chaos.py
and widen the search with CHAOS_SEEDS (how many seeds, default 6) and CHAOS_STEPS (steps
per sequence, default 40). Row ids are random, so a replay takes the same decisions but may
process due rows in a different order.
"""

import asyncio
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

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
from support_agent.db.models import Approval, Customer, Order
from support_agent.db.seed import SeedOrder, load_scenarios, seed
from support_agent.domain.enums import ApprovalDecision, JobType
from support_agent.integrations.payments import PaymentClient
from support_agent.llm.fake import ScriptStep, tool_reply
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.services import policy_search, reconciliation
from support_agent.services.invariants import check_money_invariants
from support_agent.worker import queue

HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseAgents = Callable[[dict[str, list[ScriptStep]]], AgentFake]
Drain = Callable[[], Awaitable[int]]

SEED_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
EXTRA_ORDERS = ["ORD-9001", "ORD-9002", "ORD-9003", "ORD-9004"]  # copies of Priya's ORD-1001
ORDERS = ["ORD-1001", *EXTRA_ORDERS]
FAULTS = ["connect_error", "server_error", "reply_lost", "gateway_timeout", "slow", "decline"]


def _seeds() -> list[int]:
    if replay := os.environ.get("CHAOS_SEED"):
        return [int(replay)]
    return [1000 + i for i in range(int(os.environ.get("CHAOS_SEEDS", "6")))]


STEPS = int(os.environ.get("CHAOS_STEPS", "40"))


class MutableClock:
    def __init__(self) -> None:
        self.now = SEED_NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class ChaosTransport(httpx.AsyncBaseTransport):
    """The provider, reached through an unreliable network decided by a seeded RNG."""

    def __init__(self, app: FastAPI, rng: random.Random, log: list[str]) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self._rng = rng
        self._log = log
        self.fault_rate = 0.0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._rng.random() >= self.fault_rate:
            return await self._inner.handle_async_request(request)
        fault = self._rng.choice(FAULTS)
        self._log.append(f"    fault {fault} on {request.method}")
        if fault == "connect_error":  # never reached the provider: nothing happened there
            raise httpx.ConnectError("refused", request=request)
        if fault == "server_error":  # the provider failed before doing anything
            return httpx.Response(500, json={"error": {"code": "PROVIDER_ERROR"}})
        if fault == "slow":
            await asyncio.sleep(self._rng.uniform(0.001, 0.01))
        if fault == "decline" and request.method == "POST":
            admin = httpx.Request(
                "POST", "http://payments.mock/_admin/faults", json={"mode": "decline", "count": 1}
            )
            await self._inner.handle_async_request(admin)
        response = await self._inner.handle_async_request(request)  # the work is done...
        if fault == "reply_lost":
            raise httpx.ReadTimeout("reply lost", request=request)  # ...the reply never comes
        if fault == "gateway_timeout":
            return httpx.Response(504, json={"error": {"code": "GATEWAY_TIMEOUT"}})
        return response


@dataclass
class Payments:
    app: FastAPI
    engine: AsyncEngine
    transport: ChaosTransport | None = None

    def client(self) -> PaymentClient:
        settings = Settings(
            _env_file=None,
            database_url=SecretStr("postgresql+asyncpg://user:pw@localhost:5432/unused"),
            jwt_secret=SecretStr("x" * 32),
            payments_base_url="http://payments.mock",
            provider_backoff_base_seconds=0.0,  # reads still retry, without real waiting
        )
        return PaymentClient(settings, self.transport or httpx.ASGITransport(app=self.app))

    async def rows(self) -> list[Any]:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT idempotency_key, order_reference, amount_minor, final_outcome "
                    "FROM mock_payments.refunds"
                )
            )
            return list(result)


@dataclass
class Chaos:
    seed: int
    rng: random.Random
    clock: MutableClock
    settings: Settings
    client: httpx.AsyncClient
    session: AsyncSession
    payments: Payments
    drain: Drain
    use_agents: UseAgents
    customer: dict[str, str]  # Priya's Authorization header (logging in is slow on purpose)
    reviewer: dict[str, str]  # Anita's
    citation: str
    log: list[str] = field(default_factory=list)


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


@pytest.fixture
def settings(app: FastAPI, payments: Payments, clock: MutableClock) -> Settings:
    app.dependency_overrides[get_clock] = lambda: clock

    async def payment_client() -> AsyncIterator[PaymentClient]:
        async with payments.client() as client:
            yield client

    app.dependency_overrides[get_payment_client] = payment_client
    chaos_settings = get_settings().model_copy(
        update={
            "policy_min_similarity": -1.0,
            "job_backoff_base_seconds": 0.0,
            "reconcile_backoff_base_seconds": 0.0,  # every check is due at once
            "reconcile_max_checks": 4,  # so giving up (and escalating) happens too
        }
    )
    app.dependency_overrides[get_settings] = lambda: chaos_settings
    return chaos_settings


@pytest.fixture(autouse=True)
def disarmed() -> Any:
    crash_points.disarm()
    yield
    crash_points.disarm()


async def seed_world(session: AsyncSession, settings: Settings, demo_password_hash: str) -> str:
    """The demo seed plus four copies of Priya's eligible order. Returns a policy citation."""
    data = load_scenarios()
    priya = data.customers[0]
    template: SeedOrder = priya.orders[0]
    copies = [template.model_copy(update={"order_number": number}) for number in EXTRA_ORDERS]
    priya = priya.model_copy(update={"orders": [*priya.orders, *copies]})
    data = data.model_copy(update={"customers": [priya, *data.customers[1:]]})
    await seed(session, data, SEED_NOW, demo_password_hash)

    priya_id = await session.scalar(select(Customer.id).where(Customer.email == priya.email))
    assert priya_id is not None
    found = await policy_search.search_policy(
        session, FakeEmbedder(), settings, priya_id, "return window", "ORD-1001", SEED_NOW
    )
    return found.passages[0].citation


# ---------------------------------------------------------------- one step each


def is_crash(exc: BaseException) -> bool:
    if isinstance(exc, SimulatedCrash):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return exc.subgroup(SimulatedCrash) is not None
    return False


async def surviving_crashes(chaos: Chaos, action: Awaitable[Any]) -> None:
    """Run one action; a simulated crash loses its connection's open work, as a dead process."""
    try:
        await action
    except BaseException as exc:
        if not is_crash(exc):
            raise
        chaos.log.append("    process died")
        await chaos.session.rollback()


async def open_manual_case(chaos: Chaos) -> None:
    order = chaos.rng.choice(ORDERS)
    ticket = await chaos.client.post(
        "/tickets", json={"message": "Refund please", "order_number": order}, headers=chaos.customer
    )
    proposed = await chaos.client.post(
        f"/tickets/{ticket.json()['id']}/refund-proposals", headers=chaos.reviewer
    )
    chaos.log.append(f"    manual {order}: {proposed.status_code}")


async def open_workflow_case(chaos: Chaos) -> None:
    order = chaos.rng.choice(ORDERS)
    chaos.use_agents(
        {
            "classifier": [
                tool_reply(
                    "classification", {"category": "refund", "confidence": 0.95, "reason": "t"}
                )
            ],
            "supervisor": [supervisor_reply()],
            "investigator": simple_investigation(order),
            "policy": policy_run(chaos.citation, order=order),
            "resolution": [draft_reply("refund", citations=[chaos.citation])],
            "critic": [critique_reply("approve")],
        }
    )
    ticket = await chaos.client.post(
        "/tickets", json={"message": "Refund please", "order_number": order}, headers=chaos.customer
    )
    started = await chaos.client.post(
        f"/tickets/{ticket.json()['id']}/runs", headers=chaos.customer
    )
    chaos.log.append(f"    workflow {order}: {started.status_code}")
    await chaos.drain()  # the run goes to its approval pause with this run's script


async def pending_approval(chaos: Chaos) -> Approval | None:
    stmt = (
        select(Approval)
        .where(Approval.decision == ApprovalDecision.PENDING)
        .order_by(Approval.created_at, Approval.id)
        .execution_options(populate_existing=True)
    )
    pending = list((await chaos.session.scalars(stmt)).all())
    return chaos.rng.choice(pending) if pending else None


async def decide(chaos: Chaos, decision: str) -> None:
    approval = await pending_approval(chaos)
    if approval is None:
        return
    response = await chaos.client.post(
        f"/approvals/{approval.id}/decision",
        json={"decision": decision},
        headers=chaos.reviewer,
    )
    chaos.log.append(f"    {decision}: {response.status_code}")


async def approve(chaos: Chaos) -> None:
    await decide(chaos, "approve")


async def reject(chaos: Chaos) -> None:
    await decide(chaos, "reject")


async def let_approvals_expire(chaos: Chaos) -> None:
    chaos.clock.advance(chaos.settings.approval_ttl_minutes * 60 + 1)


async def run_worker(chaos: Chaos) -> None:
    for sweep in (JobType.EXPIRE_APPROVALS, JobType.RECONCILE_REFUNDS):
        await queue.schedule_sweep(chaos.session, sweep, now=chaos.clock(), max_attempts=3)
    chaos.log.append(f"    jobs run: {await chaos.drain()}")


async def run_reconciler(chaos: Chaos) -> None:
    async with chaos.payments.client() as client:
        checked = await reconciliation.reconcile_due_refunds(
            chaos.session, client, chaos.settings, chaos.clock
        )
    chaos.log.append(f"    checked: {checked}")


async def move_clock(chaos: Chaos) -> None:
    # Sometimes a little, sometimes past a lease or the stale-reserved threshold.
    seconds = chaos.rng.choice([5, 30, chaos.settings.job_lease_seconds + 1, 301, 900])
    chaos.clock.advance(seconds)
    chaos.log.append(f"    +{seconds}s")


async def arm_a_crash(chaos: Chaos) -> None:
    point = chaos.rng.choice(list(CrashPoint))
    crash_points.arm(point)
    chaos.log.append(f"    armed {point.value}")


async def change_fault_rate(chaos: Chaos) -> None:
    assert chaos.payments.transport is not None
    chaos.payments.transport.fault_rate = chaos.rng.choice([0.0, 0.2, 0.5, 0.9])
    chaos.log.append(f"    fault rate {chaos.payments.transport.fault_rate}")


ACTIONS: list[tuple[Callable[[Chaos], Awaitable[None]], int]] = [
    (open_manual_case, 4),
    (open_workflow_case, 1),  # the real graph and checkpointer: about 1 s each
    (approve, 6),
    (reject, 1),
    (let_approvals_expire, 1),
    (run_worker, 3),
    (run_reconciler, 2),
    (move_clock, 2),
    (arm_a_crash, 2),
    (change_fault_rate, 1),
]


async def settle(chaos: Chaos) -> None:
    """The faults stop and time passes: the worker and the reconciler finish what they can."""
    crash_points.disarm()
    assert chaos.payments.transport is not None
    chaos.payments.transport.fault_rate = 0.0
    longest = max(chaos.settings.job_lease_seconds, chaos.settings.reconcile_stale_reserved_seconds)
    for _ in range(chaos.settings.reconcile_max_checks + 3):
        chaos.clock.advance(longest + 60)
        await run_worker(chaos)


async def assert_money_is_sound(chaos: Chaos) -> None:
    assert await check_money_invariants(chaos.session, grace_seconds=0) == []

    # The provider holds no refund beyond ours (keys), and never more than was paid.
    paid = dict(
        (await chaos.session.execute(select(Order.order_number, Order.paid_amount_minor))).all()
    )
    rows = await chaos.payments.rows()
    assert len({row.idempotency_key for row in rows}) == len(rows)
    paid_out: dict[str, int] = {}
    for row in rows:
        if row.final_outcome == "succeeded":
            paid_out[row.order_reference] = paid_out.get(row.order_reference, 0) + row.amount_minor
    for order_number, total in paid_out.items():
        assert total <= paid[order_number], f"{order_number} paid out {total}"


# ---------------------------------------------------------------- the test


@pytest.mark.parametrize("seed_value", _seeds(), ids=lambda value: f"seed={value}")
async def test_money_chaos(
    seed_value: int,
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    clock: MutableClock,
    settings: Settings,
    drain_jobs: Drain,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    demo_password_hash: str,
    ingested_policies: None,
) -> None:
    citation = await seed_world(session, settings, demo_password_hash)
    rng = random.Random(seed_value)  # noqa: S311  (replayable test chaos, not security)
    log: list[str] = []
    payments.transport = ChaosTransport(payments.app, rng, log)
    payments.transport.fault_rate = 0.3
    chaos = Chaos(
        seed=seed_value,
        rng=rng,
        clock=clock,
        settings=settings,
        client=client,
        session=session,
        payments=payments,
        drain=drain_jobs,
        use_agents=use_agents,
        customer=await auth_headers("priya@example.com"),
        reviewer=await auth_headers("anita@example.com"),
        citation=citation,
        log=log,
    )
    actions, weights = zip(*ACTIONS, strict=True)
    try:
        for step in range(STEPS):
            action = rng.choices(actions, weights)[0]
            log.append(f"{step}: {action.__name__}")
            await surviving_crashes(chaos, action(chaos))
        log.append("settle")
        await settle(chaos)
        await assert_money_is_sound(chaos)
    except BaseException as exc:
        exc.add_note(f"chaos seed {seed_value} (replay: CHAOS_SEED={seed_value})")
        exc.add_note("steps:\n" + "\n".join(log))
        raise
