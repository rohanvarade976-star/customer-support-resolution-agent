"""Money races: two actors on the same approval or refund at the same moment (ADR-015).

Most tests here use `committed_world`: the seed is committed, and each actor gets its own
session on its own connection, so row locks, SKIP LOCKED and compare-and-set writes behave
as they do in production. The interleavings are forced, not left to luck: a wrapper holds
one actor at the dangerous moment (an asyncio.Event or Barrier) while the other acts.

The last two tests are about ordering rather than locks: the reconciler settles a refund
in the gap between the workflow's refund step and the moment that segment is saved. They
run on the usual rolled-back session with the real workflow.

Every test ends with the invariant checker and the provider's refund count.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
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
from support_agent.core.config import Settings, get_settings
from support_agent.core.errors import AppError
from support_agent.db.models import (
    ActionProposal,
    Approval,
    AuditEvent,
    Customer,
    Job,
    Order,
    RefundOperation,
    StaffUser,
    Ticket,
    WorkflowRun,
)
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import (
    ApprovalDecision,
    JobType,
    ProposalStatus,
    RefundOperationStatus,
    Role,
    RunMode,
    RunStatus,
    TicketStatus,
)
from support_agent.integrations.payments import PaymentClient
from support_agent.llm.fake import ScriptStep, tool_reply
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.repositories import proposals as proposal_repo
from support_agent.schemas.approvals import DecisionRequest
from support_agent.schemas.auth import Principal
from support_agent.services import approvals, policy_search, reconciliation, refund_execution
from support_agent.services import proposals as proposal_service
from support_agent.services import runs as runs_service
from support_agent.services.invariants import check_money_invariants
from support_agent.services.run_recording import save_segment
from support_agent.services.runs import EnqueueEnv
from support_agent.workflow.graph import Checkpointer

Sessions = async_sessionmaker[AsyncSession]
HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseAgents = Callable[[dict[str, list[ScriptStep]]], AgentFake]
Drain = Callable[[], Awaitable[int]]

SEED_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PRIYA_AMOUNT = 249900  # ORD-1001, paise
HOLD = 0.3  # seconds one actor holds its lock while the other tries


class MutableClock:
    def __init__(self, now: datetime = SEED_NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class HoldGets(httpx.AsyncBaseTransport):
    """Lets POSTs through; holds every GET (a reconciler's lookup) until released."""

    def __init__(self, app: FastAPI) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.waiting.set()
            await self.release.wait()
        return await self._inner.handle_async_request(request)


@dataclass
class Payments:
    app: FastAPI
    engine: AsyncEngine

    def client(self, transport: httpx.AsyncBaseTransport | None = None) -> PaymentClient:
        settings = Settings(
            _env_file=None,
            database_url=SecretStr("postgresql+asyncpg://user:pw@localhost:5432/unused"),
            jwt_secret=SecretStr("x" * 32),
            payments_base_url="http://payments.mock",
            provider_max_attempts=1,
        )
        return PaymentClient(settings, transport or httpx.ASGITransport(app=self.app))

    async def refund_count(self) -> int:
        async with self.engine.connect() as conn:
            return int(await conn.scalar(text("SELECT count(*) FROM mock_payments.refunds")) or 0)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def provider_clock() -> MutableClock:
    """The provider's own clock: moved forward to let it finish a refund."""
    return MutableClock()


@pytest.fixture
async def payments(test_database_url: str, provider_clock: MutableClock) -> AsyncIterator[Payments]:
    settings = PaymentsSettings(app_env="test", database_url=SecretStr(test_database_url))
    app = create_payments_app(settings, clock=provider_clock)
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    async with app.router.lifespan_context(app):
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE mock_payments.refunds"))
        yield Payments(app, engine)
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(
        update={"reconcile_backoff_base_seconds": 0.0, "policy_min_similarity": -1.0}
    )


# ---------------------------------------------------------------- committed-world helpers


async def reviewer(db: AsyncSession) -> Principal:
    staff = await db.scalar(select(StaffUser).where(StaffUser.email == "anita@example.com"))
    assert staff is not None
    return Principal(id=staff.id, role=Role.REVIEWER, name=staff.name, email=staff.email)


async def priya_ticket(db: AsyncSession) -> Ticket:
    customer = await db.scalar(select(Customer).where(Customer.email == "priya@example.com"))
    order = await db.scalar(select(Order).where(Order.order_number == "ORD-1001"))
    assert customer is not None and order is not None
    ticket = Ticket(customer_id=customer.id, order_id=order.id, summary="race test")
    db.add(ticket)
    await db.flush()
    return ticket


async def manual_proposal(sessions: Sessions, settings: Settings, now: datetime) -> UUID:
    """Priya's refund proposed by a reviewer (a manual run, approval pending). Committed."""
    async with sessions() as db:
        ticket = await priya_ticket(db)
        await db.commit()
        proposed = await proposal_service.create_refund_proposal(
            db, await reviewer(db), ticket.id, now=now, settings=settings
        )
    return proposed.approval_id


async def mark_approved(sessions: Sessions, approval_id: UUID, now: datetime) -> UUID:
    """Store the reviewer's approval exactly as the decision service does before executing."""
    async with sessions() as db:
        approval = await db.get(Approval, approval_id)
        assert approval is not None
        proposal = await db.get(ActionProposal, approval.proposal_id)
        assert proposal is not None
        run = await db.get(WorkflowRun, proposal.run_id)
        assert run is not None
        await proposal_repo.record_decision(
            db,
            approval,
            decision=ApprovalDecision.APPROVED,
            reviewer_id=str((await reviewer(db)).id),
            decided_at=now,
        )
        await proposal_repo.set_proposal_status(db, proposal, ProposalStatus.APPROVED)
        run.status = RunStatus.EXECUTING
        await db.commit()
        return proposal.id


async def workflow_approval(sessions: Sessions, settings: Settings, now: datetime) -> Approval:
    """A workflow run paused at Priya's approval, as the graph leaves it. Committed."""
    async with sessions() as db:
        ticket = await priya_ticket(db)
        run = WorkflowRun(
            ticket_id=ticket.id,
            thread_id=f"race-{uuid4().hex}",
            mode=RunMode.MULTI_AGENT,
            status=RunStatus.AWAITING_APPROVAL,
        )
        db.add(run)
        await db.flush()
        proposal_id = await proposal_service.create_proposal_for_run(
            db,
            ticket,
            run.id,
            order_number="ORD-1001",
            expected_amount_minor=PRIYA_AMOUNT,
            now=now,
            settings=settings,
        )
        await db.commit()
        approval = await db.scalar(select(Approval).where(Approval.proposal_id == proposal_id))
        assert approval is not None
        return approval


async def count(db: AsyncSession, stmt: Any) -> int:
    return int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


async def event_count(db: AsyncSession, event_type: str) -> int:
    return await count(db, select(AuditEvent.id).where(AuditEvent.event_type == event_type))


async def refund_operations(db: AsyncSession) -> list[RefundOperation]:
    return list(
        (
            await db.scalars(
                select(RefundOperation).where(RefundOperation.idempotency_key.like("refund-%"))
            )
        ).all()
    )


async def assert_sound(sessions: Sessions, payments: Payments, *, provider_refunds: int) -> None:
    async with sessions() as db:
        assert await check_money_invariants(db, grace_seconds=0) == []
    assert await payments.refund_count() == provider_refunds


# ---------------------------------------------------------------- 1. the same approval twice


async def test_two_executions_of_one_approval_give_exactly_one_refund(
    committed_world: None,
    committed_sessions: Sessions,
    payments: Payments,
    settings: Settings,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_id = await manual_proposal(committed_sessions, settings, clock())
    proposal_id = await mark_approved(committed_sessions, approval_id, clock())

    # Both executors pass revalidation and the early "already executed?" check before
    # either reserves: only the order lock and the unique constraint stand between them.
    both_checked = asyncio.Barrier(2)
    real_reserve = refund_execution._reserve

    async def reserve_together(*args: Any, **kwargs: Any) -> Any:
        await both_checked.wait()
        return await real_reserve(*args, **kwargs)

    monkeypatch.setattr(refund_execution, "_reserve", reserve_together)

    async def execute() -> UUID:
        async with committed_sessions() as db, payments.client() as client:
            operation = await refund_execution.execute_approved_refund(
                db, client, proposal_id, now=clock(), settings=settings
            )
            return operation.id

    first, second = await asyncio.gather(execute(), execute())

    assert first == second  # the loser got the winner's operation back
    async with committed_sessions() as db:
        assert len(await refund_operations(db)) == 1
        assert await event_count(db, "refund.reserved") == 1
    await assert_sound(committed_sessions, payments, provider_refunds=1)


async def test_two_reviewers_approving_at_once_give_exactly_one_refund(
    committed_world: None,
    committed_sessions: Sessions,
    payments: Payments,
    settings: Settings,
    clock: MutableClock,
    checkpointer: Checkpointer,
) -> None:
    approval_id = await manual_proposal(committed_sessions, settings, clock())
    env = EnqueueEnv(settings, clock, checkpointer)
    body = DecisionRequest(decision="approve")

    async def decide() -> str:
        async with committed_sessions() as db, payments.client() as client:
            try:
                await approvals.decide(db, await reviewer(db), approval_id, body, env, client)
            except AppError as exc:
                return exc.error_code
            return "approved"

    results = sorted(await asyncio.gather(decide(), decide()))

    assert results == ["ALREADY_DECIDED", "approved"]
    async with committed_sessions() as db:
        assert len(await refund_operations(db)) == 1
        assert await event_count(db, "approval.approved") == 1
    await assert_sound(committed_sessions, payments, provider_refunds=1)


# ---------------------------------------------------------------- 2. approve versus expiry


@pytest.mark.parametrize("first", ["reviewer", "expiry_sweep"])
async def test_approve_and_expiry_at_the_same_moment_give_exactly_one_outcome(
    first: str,
    committed_world: None,
    committed_sessions: Sessions,
    payments: Payments,
    settings: Settings,
    checkpointer: Checkpointer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval = await workflow_approval(committed_sessions, settings, SEED_NOW)
    # The reviewer is one millisecond early; the sweep, one millisecond late. Both act.
    reviewer_env = EnqueueEnv(
        settings, lambda: approval.expires_at - timedelta(milliseconds=1), checkpointer
    )
    sweep_env = EnqueueEnv(
        settings, lambda: approval.expires_at + timedelta(milliseconds=1), checkpointer
    )

    # Whoever goes first takes the approval's row lock and holds it while the other tries.
    locked = asyncio.Event()

    def holding(real: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            row = await real(*args, **kwargs)
            if row is not None and not locked.is_set():
                locked.set()
                await asyncio.sleep(HOLD)
            return row

        return wrapper

    target = "lock_approval" if first == "reviewer" else "lock_next_expired_workflow_approval"
    monkeypatch.setattr(proposal_repo, target, holding(getattr(proposal_repo, target)))

    async def reviewer_decides() -> str:
        if first != "reviewer":
            await locked.wait()
        async with committed_sessions() as db, payments.client() as client:
            body = DecisionRequest(decision="approve")
            try:
                await approvals.decide(
                    db, await reviewer(db), approval.id, body, reviewer_env, client
                )
            except AppError as exc:
                return exc.error_code
            return "approved"

    async def sweep() -> int:
        if first == "reviewer":
            await locked.wait()
        async with committed_sessions() as db:
            return await approvals.expire_overdue_approvals(db, sweep_env)

    decided, expired = await asyncio.gather(reviewer_decides(), sweep())

    async with committed_sessions() as db:
        stored = await db.get(Approval, approval.id)
        assert stored is not None
        if first == "reviewer":
            assert (decided, expired) == ("approved", 0)  # the sweep skipped the locked row
            assert stored.decision == ApprovalDecision.APPROVED
        else:
            assert (decided, expired) == ("ALREADY_DECIDED", 1)
            assert stored.decision == ApprovalDecision.EXPIRED
        # Exactly one outcome: one decision event, one claim of the run, one resume job.
        decisions = select(AuditEvent.id).where(
            AuditEvent.event_type.in_(["approval.approved", "approval.expired"])
        )
        assert await count(db, decisions) == 1
        assert await event_count(db, "run.resumed") == 1
        resume_jobs = select(Job.id).where(Job.job_type == JobType.RESUME_AFTER_DECISION)
        assert await count(db, resume_jobs) == 1
        run = await db.scalar(select(WorkflowRun).where(WorkflowRun.thread_id.like("race-%")))
        assert run is not None and run.status == RunStatus.EXECUTING
        assert await count(db, select(RefundOperation.id)) == 1  # only the seeded history
    await assert_sound(committed_sessions, payments, provider_refunds=0)


# ---------------------------------------------------------------- 3. executor versus reconciler


async def test_a_late_executor_never_overwrites_what_the_reconciler_settled(
    committed_world: None,
    committed_sessions: Sessions,
    payments: Payments,
    settings: Settings,
    clock: MutableClock,
    provider_clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_id = await manual_proposal(committed_sessions, settings, clock())
    proposal_id = await mark_approved(committed_sessions, approval_id, clock())

    # The executor's refund reaches the provider, then the executor stalls before recording.
    sent, resume = asyncio.Event(), asyncio.Event()
    real_send = refund_execution.send_refund

    async def send_then_stall(*args: Any, **kwargs: Any) -> Any:
        outcome = await real_send(*args, **kwargs)
        sent.set()
        await resume.wait()
        return outcome

    monkeypatch.setattr(refund_execution, "send_refund", send_then_stall)

    async def executor() -> None:
        async with committed_sessions() as db, payments.client() as client:
            await refund_execution.execute_approved_refund(
                db, client, proposal_id, now=clock(), settings=settings
            )

    async def reconciler() -> None:
        await sent.wait()
        # Long enough later that the reservation counts as stale, and the provider is done.
        later = MutableClock(
            clock() + timedelta(seconds=settings.reconcile_stale_reserved_seconds + 1)
        )
        provider_clock.advance(60)
        async with committed_sessions() as db, payments.client() as client:
            assert await reconciliation.reconcile_due_refunds(db, client, settings, later) == 1
        resume.set()

    await asyncio.gather(executor(), reconciler())

    async with committed_sessions() as db:
        (operation,) = await refund_operations(db)
        assert operation.status == RefundOperationStatus.SUCCEEDED  # the reconciler's answer
        assert operation.resent_at is None  # found by key: never sent again
        assert await event_count(db, "refund.succeeded") == 1
        assert await event_count(db, "refund.submitted") == 0  # the executor wrote nothing
        ticket = await db.scalar(select(Ticket).where(Ticket.summary == "race test"))
        assert ticket is not None and ticket.status == TicketStatus.RESOLVED
    await assert_sound(committed_sessions, payments, provider_refunds=1)


async def test_a_late_reconciler_never_overwrites_what_the_executor_recorded(
    committed_world: None,
    committed_sessions: Sessions,
    payments: Payments,
    settings: Settings,
    clock: MutableClock,
    provider_clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_id = await manual_proposal(committed_sessions, settings, clock())
    proposal_id = await mark_approved(committed_sessions, approval_id, clock())
    later = MutableClock(clock() + timedelta(seconds=settings.reconcile_stale_reserved_seconds + 1))

    # The executor sends, then stalls until the reconciler has claimed the stale reservation
    # and is itself stuck waiting for the provider's answer to its lookup.
    sent, resume = asyncio.Event(), asyncio.Event()
    real_send = refund_execution.send_refund

    async def send_then_stall(*args: Any, **kwargs: Any) -> Any:
        outcome = await real_send(*args, **kwargs)
        sent.set()
        await resume.wait()
        return outcome

    monkeypatch.setattr(refund_execution, "send_refund", send_then_stall)
    held = HoldGets(payments.app)

    async def executor() -> None:
        async with committed_sessions() as db, payments.client() as client:
            await refund_execution.execute_approved_refund(
                db, client, proposal_id, now=clock(), settings=settings
            )
        held.release.set()  # the reconciler's lookup answers only now

    async def reconciler() -> None:
        await sent.wait()
        provider_clock.advance(60)
        async with committed_sessions() as db, payments.client(held) as client:
            claimed = asyncio.create_task(
                reconciliation.reconcile_due_refunds(db, client, settings, later)
            )
            await held.waiting.wait()  # claimed, and its lookup is in flight
            resume.set()
            assert await claimed == 1

    await asyncio.gather(executor(), reconciler())

    async with committed_sessions() as db:
        (operation,) = await refund_operations(db)
        # The executor's answer stands; the reconciler's stale claim wrote nothing.
        assert operation.status == RefundOperationStatus.SUBMITTED
        assert operation.reconcile_checks == 1
        assert await event_count(db, "refund.reconciled") == 0
        assert operation.next_reconcile_at is not None  # still watched

    # The next sweep settles it normally.
    later.advance(1)
    async with committed_sessions() as db, payments.client() as client:
        await reconciliation.reconcile_due_refunds(db, client, settings, later)
    async with committed_sessions() as db:
        (operation,) = await refund_operations(db)
        assert operation.status == RefundOperationStatus.SUCCEEDED
    await assert_sound(committed_sessions, payments, provider_refunds=1)


# ---------------------------------------------------------------- 4. the workflow's save


@dataclass(frozen=True)
class WorkflowCase:
    ticket_id: UUID
    run_id: UUID
    proposal_id: UUID


@pytest.fixture
def workflow_app(
    app: FastAPI, payments: Payments, clock: MutableClock, settings: Settings
) -> Settings:
    app.dependency_overrides[get_clock] = lambda: clock

    async def payment_client() -> AsyncIterator[PaymentClient]:
        async with payments.client() as client:
            yield client

    app.dependency_overrides[get_payment_client] = payment_client
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def approved_workflow_run(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    drain_jobs: Drain,
    use_agents: UseAgents,
    settings: Settings,
) -> WorkflowCase:
    priya_id = await session.scalar(
        select(Customer.id).where(Customer.email == "priya@example.com")
    )
    assert priya_id is not None
    found = await policy_search.search_policy(
        session, FakeEmbedder(), settings, priya_id, "return window", "ORD-1001", SEED_NOW
    )
    citation = found.passages[0].citation
    use_agents(
        {
            "classifier": [
                tool_reply(
                    "classification", {"category": "refund", "confidence": 0.95, "reason": "t"}
                )
            ],
            "supervisor": [supervisor_reply()],
            "investigator": simple_investigation("ORD-1001"),
            "policy": policy_run(citation),
            "resolution": [draft_reply("refund", citations=[citation])],
            "critic": [critique_reply("approve")],
        }
    )
    priya = await auth_headers("priya@example.com")
    created = await client.post(
        "/tickets", json={"message": "Refund please", "order_number": "ORD-1001"}, headers=priya
    )
    ticket_id = UUID(created.json()["id"])
    started = await client.post(f"/tickets/{ticket_id}/runs", headers=priya)
    run_id = UUID(started.json()["run_id"])
    await drain_jobs()
    proposal = await session.scalar(select(ActionProposal).where(ActionProposal.run_id == run_id))
    assert proposal is not None
    approval = await session.scalar(select(Approval).where(Approval.proposal_id == proposal.id))
    assert approval is not None
    decided = await client.post(
        f"/approvals/{approval.id}/decision",
        json={"decision": "approve"},
        headers=await auth_headers("anita@example.com"),
    )
    assert decided.status_code == 202
    return WorkflowCase(ticket_id, run_id, proposal.id)


def reconcile_before_the_segment_is_saved(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    payments: Payments,
    settings: Settings,
    when: MutableClock,
) -> None:
    """The reconciler runs after the refund step committed, before the segment is saved."""

    async def reconcile_then_save(*args: Any, **kwargs: Any) -> None:
        async with payments.client() as client:
            await reconciliation.reconcile_due_refunds(session, client, settings, when)
        await save_segment(*args, **kwargs)

    monkeypatch.setattr(runs_service, "save_segment", reconcile_then_save)


async def test_the_saved_segment_never_undoes_a_refund_the_reconciler_completed(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    clock: MutableClock,
    provider_clock: MutableClock,
    drain_jobs: Drain,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    workflow_app: Settings,
    demo_password_hash: str,
    ingested_policies: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(session, load_scenarios(), SEED_NOW, demo_password_hash)
    case = await approved_workflow_run(
        client, session, auth_headers, drain_jobs, use_agents, workflow_app
    )
    # The executor's answer will be "submitted"; by the time the reconciler looks, the
    # provider has finished the refund.
    real_send = refund_execution.send_refund

    async def send_and_let_time_pass(*args: Any, **kwargs: Any) -> Any:
        outcome = await real_send(*args, **kwargs)
        provider_clock.advance(60)
        return outcome

    monkeypatch.setattr(refund_execution, "send_refund", send_and_let_time_pass)
    reconcile_before_the_segment_is_saved(monkeypatch, session, payments, workflow_app, clock)

    await drain_jobs()

    operation = await session.scalar(
        select(RefundOperation)
        .where(RefundOperation.proposal_id == case.proposal_id)
        .execution_options(populate_existing=True)
    )
    assert operation is not None and operation.status == RefundOperationStatus.SUCCEEDED
    ticket = await session.get(Ticket, case.ticket_id, populate_existing=True)
    assert ticket is not None
    assert ticket.status == TicketStatus.RESOLVED  # not set back to "in progress"
    assert ticket.customer_reply == refund_execution.describe(RefundOperationStatus.SUCCEEDED)
    run = await session.get(WorkflowRun, case.run_id, populate_existing=True)
    assert run is not None and run.status == RunStatus.COMPLETED
    assert await check_money_invariants(session, grace_seconds=0) == []
    assert await payments.refund_count() == 1


async def test_the_saved_segment_never_takes_an_escalated_run_back_from_its_person(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    clock: MutableClock,
    drain_jobs: Drain,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    workflow_app: Settings,
    demo_password_hash: str,
    ingested_policies: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(session, load_scenarios(), SEED_NOW, demo_password_hash)
    case = await approved_workflow_run(
        client, session, auth_headers, drain_jobs, use_agents, workflow_app
    )

    # The refund's reply is lost, and the reconciler's one allowed check learns nothing,
    # so it gives up and hands the case to a person, all before the segment is saved.
    class LoseEverything(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._inner = httpx.ASGITransport(app=payments.app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await self._inner.handle_async_request(request)  # the provider does the work...
            raise httpx.ReadTimeout("reply lost", request=request)  # ...the reply never comes

    lossy = LoseEverything()
    monkeypatch.setattr(payments, "client", lambda transport=None: Payments.client(payments, lossy))
    one_check = workflow_app.model_copy(update={"reconcile_max_checks": 1})
    reconcile_before_the_segment_is_saved(monkeypatch, session, payments, one_check, clock)

    await drain_jobs()

    operation = await session.scalar(
        select(RefundOperation)
        .where(RefundOperation.proposal_id == case.proposal_id)
        .execution_options(populate_existing=True)
    )
    assert operation is not None
    assert operation.status == RefundOperationStatus.PENDING_RECONCILIATION  # untouched
    run = await session.get(WorkflowRun, case.run_id, populate_existing=True)
    assert run is not None and run.status == RunStatus.ESCALATED  # still with its person
    ticket = await session.get(Ticket, case.ticket_id, populate_existing=True)
    assert ticket is not None and ticket.status == TicketStatus.ESCALATED
    assert await check_money_invariants(session, grace_seconds=0) == []
