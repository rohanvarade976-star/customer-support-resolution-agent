"""Refund reconciliation against the mock payment provider and its faults (ADR-014).

Each test approves Priya's refund through the API (a manual run, executed in the request),
breaks something on the way, then lets the reconciler settle it. Money is checked in our
refund_operations table and in the provider's own table (`refund_count`), never in text.

The provider's clock is the test's clock, so "submitted -> processing -> succeeded" (2 s,
then 5 s) happens when a test moves time forward, not in real time.
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
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from mock_providers.payments.app import create_app as create_payments_app
from mock_providers.payments.config import PaymentsSettings
from support_agent.agents.resolve import ESCALATION_MESSAGE
from support_agent.api.deps import get_clock, get_payment_client
from support_agent.core.config import Settings, get_settings
from support_agent.db.models import (
    ActionProposal,
    AuditEvent,
    CustomerMemory,
    RefundOperation,
    Ticket,
    WorkflowRun,
)
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import (
    JobType,
    MemoryKind,
    MemoryOutcome,
    ProposalStatus,
    RefundOperationStatus,
    RunStatus,
    TicketStatus,
)
from support_agent.integrations.payments import PaymentClient
from support_agent.repositories import refunds
from support_agent.services import reconciliation, refund_execution
from support_agent.services.runs import escalate_after_dead_job
from support_agent.worker import queue

Headers = dict[str, str]
AuthHeaders = Callable[[str], Awaitable[Headers]]

REVIEWER = "anita@example.com"
PRIYA_AMOUNT = 249900  # ORD-1001, paise


# ---------------------------------------------------------------- fixtures


class MutableClock:
    """The clock of the app, the reconciler and the mock provider alike."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


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
            provider_max_attempts=1,  # one read per check: faults are counted exactly
        )
        return PaymentClient(settings, transport or httpx.ASGITransport(app=self.app))

    async def refund_count(self) -> int:
        async with self.engine.connect() as conn:
            return int(await conn.scalar(text("SELECT count(*) FROM mock_payments.refunds")) or 0)

    async def set_fault(self, **config: Any) -> None:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://admin") as admin:
            response = await admin.post("/_admin/faults", json=config)
        assert response.status_code == 200


class LosePostReplies(httpx.AsyncBaseTransport):
    """The provider handles every request, but the reply to a POST never arrives."""

    def __init__(self, app: FastAPI) -> None:
        self._inner = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("reply lost", request=request)
        return response


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture(autouse=True)
async def seeded(
    session: AsyncSession, demo_password_hash: str, clock: MutableClock, ingested_policies: None
) -> None:
    await seed(session, load_scenarios(), clock.now, demo_password_hash)


@pytest.fixture
async def payments(test_database_url: str, clock: MutableClock) -> AsyncIterator[Payments]:
    settings = PaymentsSettings(app_env="test", database_url=SecretStr(test_database_url))
    app = create_payments_app(settings, clock=clock)
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    async with app.router.lifespan_context(app):  # ASGITransport does not run lifespan
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE mock_payments.refunds"))
        yield Payments(app, engine)
    await engine.dispose()


@pytest.fixture(autouse=True)
def wire_app(app: FastAPI, payments: Payments, clock: MutableClock) -> None:
    app.dependency_overrides[get_clock] = lambda: clock

    async def payment_client() -> AsyncIterator[PaymentClient]:
        async with payments.client() as client:
            yield client

    app.dependency_overrides[get_payment_client] = payment_client


@pytest.fixture
def settings() -> Settings:
    """No backoff (every check is due at once) and three checks before escalating."""
    return get_settings().model_copy(
        update={"reconcile_backoff_base_seconds": 0.0, "reconcile_max_checks": 3}
    )


# ---------------------------------------------------------------- helpers


@dataclass(frozen=True)
class Case:
    proposal_id: UUID
    run_id: UUID
    ticket_id: UUID


async def approve_priya(client: httpx.AsyncClient, auth_headers: AuthHeaders) -> Case:
    """Open Priya's ticket, propose the refund, approve it (executed in the request)."""
    response = await client.post(
        "/tickets",
        json={"message": "I would like a refund", "order_number": "ORD-1001"},
        headers=await auth_headers("priya@example.com"),
    )
    assert response.status_code == 201
    reviewer = await auth_headers(REVIEWER)
    proposed = await client.post(
        f"/tickets/{response.json()['id']}/refund-proposals", headers=reviewer
    )
    assert proposed.status_code == 201
    body = proposed.json()
    decided = await client.post(
        f"/approvals/{body['approval_id']}/decision", json={"decision": "approve"}, headers=reviewer
    )
    assert decided.status_code == 200
    return Case(UUID(body["proposal_id"]), UUID(body["run_id"]), UUID(body["ticket_id"]))


async def reconcile(
    session: AsyncSession, payments: Payments, settings: Settings, clock: MutableClock
) -> int:
    async with payments.client() as client:
        return await reconciliation.reconcile_due_refunds(session, client, settings, clock)


async def operation_for(session: AsyncSession, case: Case) -> RefundOperation:
    row = await refunds.get_for_proposal(session, case.proposal_id)
    assert row is not None
    return row


async def reload[M](session: AsyncSession, model: type[M], row_id: UUID) -> M:
    row = await session.get(model, row_id, populate_existing=True)
    assert row is not None
    return row


async def count_events(session: AsyncSession, run_id: UUID, event_type: str) -> int:
    stmt = select(func.count()).where(
        AuditEvent.run_id == run_id, AuditEvent.event_type == event_type
    )
    return int(await session.scalar(stmt) or 0)


async def memory_for(session: AsyncSession, run_id: UUID) -> CustomerMemory | None:
    stmt = select(CustomerMemory).where(CustomerMemory.source_run_id == run_id)
    return await session.scalar(stmt.execution_options(populate_existing=True))


async def assert_resolved_with_success(session: AsyncSession, case: Case) -> None:
    operation = await operation_for(session, case)
    assert operation.status == RefundOperationStatus.SUCCEEDED
    assert operation.next_reconcile_at is None  # nothing left to check
    ticket = await reload(session, Ticket, case.ticket_id)
    assert ticket.status == TicketStatus.RESOLVED
    assert ticket.customer_reply == refund_execution.describe(RefundOperationStatus.SUCCEEDED)
    assert (await reload(session, WorkflowRun, case.run_id)).status == RunStatus.COMPLETED
    proposal = await reload(session, ActionProposal, case.proposal_id)
    assert proposal.status == ProposalStatus.EXECUTED
    item = await memory_for(session, case.run_id)
    assert item is not None
    assert (item.kind, item.outcome_code) == (
        MemoryKind.REFUND_EXECUTED,
        MemoryOutcome.REFUND_SUCCEEDED,
    )
    assert await count_events(session, case.run_id, "refund.succeeded") == 1


# ---------------------------------------------------------------- the provider's lag


async def test_submitted_refund_is_reconciled_and_ticket_resolved_only_on_success(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    settings: Settings,
    clock: MutableClock,
) -> None:
    case = await approve_priya(client, auth_headers)
    assert (await operation_for(session, case)).status == RefundOperationStatus.SUBMITTED

    # Still submitted at the provider: nothing new, the ticket is not resolved.
    assert await reconcile(session, payments, settings, clock) == 1
    assert (await operation_for(session, case)).status == RefundOperationStatus.SUBMITTED
    assert (await reload(session, Ticket, case.ticket_id)).status == TicketStatus.IN_PROGRESS

    # Processing: adopted, and still not resolved.
    clock.advance(3)
    await reconcile(session, payments, settings, clock)
    assert (await operation_for(session, case)).status == RefundOperationStatus.PROCESSING
    ticket = await reload(session, Ticket, case.ticket_id)
    assert ticket.status == TicketStatus.IN_PROGRESS
    assert ticket.customer_reply == refund_execution.describe(RefundOperationStatus.PROCESSING)

    # The provider confirms: now, and only now, the ticket is resolved.
    clock.advance(10)
    await reconcile(session, payments, settings, clock)
    await assert_resolved_with_success(session, case)
    assert await payments.refund_count() == 1

    # Settled: later sweeps find nothing to do.
    clock.advance(3600)
    assert await reconcile(session, payments, settings, clock) == 0


async def test_refund_the_provider_declines_later_fails_and_escalates(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    settings: Settings,
    clock: MutableClock,
) -> None:
    await payments.set_fault(mode="decline", count=1)
    case = await approve_priya(client, auth_headers)
    clock.advance(10)

    await reconcile(session, payments, settings, clock)

    assert (await operation_for(session, case)).status == RefundOperationStatus.FAILED
    assert (await reload(session, Ticket, case.ticket_id)).status == TicketStatus.ESCALATED
    assert (await reload(session, WorkflowRun, case.run_id)).status == RunStatus.FAILED
    item = await memory_for(session, case.run_id)
    assert item is not None and item.outcome_code == MemoryOutcome.REFUND_FAILED


# ---------------------------------------------------------------- unknown outcomes


async def test_dropped_response_is_found_by_key_and_adopted(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    settings: Settings,
    clock: MutableClock,
) -> None:
    await payments.set_fault(mode="drop_response", count=1)
    case = await approve_priya(client, auth_headers)
    operation = await operation_for(session, case)
    assert operation.status == RefundOperationStatus.PENDING_RECONCILIATION
    assert operation.provider_reference is None

    clock.advance(10)  # the provider has finished it by now
    await reconcile(session, payments, settings, clock)

    operation = await operation_for(session, case)
    assert operation.provider_reference is not None
    assert operation.resent_at is None  # found by key: nothing was sent again
    await assert_resolved_with_success(session, case)
    assert await count_events(session, case.run_id, "refund.reconciled") == 1
    assert await payments.refund_count() == 1


async def test_missing_refund_is_resent_once_with_the_same_key(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    settings: Settings,
    clock: MutableClock,
) -> None:
    # A 500 before the provider stored anything: the outcome is unknown to us, but there
    # is in fact no refund.
    await payments.set_fault(mode="error", count=1)
    case = await approve_priya(client, auth_headers)
    assert (await operation_for(session, case)).status == (
        RefundOperationStatus.PENDING_RECONCILIATION
    )
    assert await payments.refund_count() == 0

    await reconcile(session, payments, settings, clock)  # 404 by key -> re-send

    operation = await operation_for(session, case)
    assert operation.resent_at is not None
    assert operation.status == RefundOperationStatus.SUBMITTED
    assert await payments.refund_count() == 1
    async with payments.client() as client_:
        found = await client_.find_refund_by_key(operation.idempotency_key)
    assert found.idempotency_key == refund_execution.idempotency_key_for(case.proposal_id)
    assert found.amount_minor == PRIYA_AMOUNT

    clock.advance(10)
    await reconcile(session, payments, settings, clock)
    await assert_resolved_with_success(session, case)
    assert await count_events(session, case.run_id, "refund.resent") == 1
    assert await payments.refund_count() == 1  # exactly one refund at the provider


async def test_resend_whose_reply_is_lost_is_found_and_never_sent_again(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    settings: Settings,
    clock: MutableClock,
) -> None:
    await payments.set_fault(mode="error", count=1)  # no refund at the provider
    case = await approve_priya(client, auth_headers)

    # Lookup -> 404 -> re-send, which the provider stores but whose reply is lost.
    async with payments.client(LosePostReplies(payments.app)) as lossy:
        await reconciliation.reconcile_due_refunds(session, lossy, settings, clock)
    operation = await operation_for(session, case)
    assert operation.status == RefundOperationStatus.PENDING_RECONCILIATION
    assert operation.resent_at is not None
    assert await payments.refund_count() == 1

    # The next check finds it by key; it is never sent a third time.
    clock.advance(10)
    await reconcile(session, payments, settings, clock)
    await assert_resolved_with_success(session, case)
    assert await count_events(session, case.run_id, "refund.resent") == 1
    assert await payments.refund_count() == 1


async def test_stale_reserved_refund_is_recovered(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    settings: Settings,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The process dies after reserving, before the provider is called.
    async def crash(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("worker died")

    monkeypatch.setattr(refund_execution, "send_refund", crash)
    with pytest.raises(RuntimeError):
        await approve_priya(client, auth_headers)
    monkeypatch.undo()

    reserved = select(RefundOperation).where(
        RefundOperation.status == RefundOperationStatus.RESERVED
    )
    [operation] = (await session.scalars(reserved)).all()
    assert operation.status == RefundOperationStatus.RESERVED
    assert await payments.refund_count() == 0

    # Not stale yet: the executor might still be talking to the provider.
    assert await reconcile(session, payments, settings, clock) == 0

    clock.advance(get_settings().reconcile_stale_reserved_seconds + 1)
    assert await reconcile(session, payments, settings, clock) == 1  # 404 -> sent once

    resent = await reload(session, RefundOperation, operation.id)
    assert resent.status == RefundOperationStatus.SUBMITTED
    assert resent.resent_at is not None
    assert await payments.refund_count() == 1

    clock.advance(10)
    await reconcile(session, payments, settings, clock)
    settled = await reload(session, RefundOperation, operation.id)
    assert settled.status == RefundOperationStatus.SUCCEEDED
    proposal = await reload(session, ActionProposal, settled.proposal_id)
    run = await reload(session, WorkflowRun, proposal.run_id)
    assert run.status == RunStatus.COMPLETED
    assert (await reload(session, Ticket, run.ticket_id)).status == TicketStatus.RESOLVED
    assert await payments.refund_count() == 1


# ---------------------------------------------------------------- giving up safely


async def test_repeated_provider_errors_back_off_then_escalate_without_moving_money(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    clock: MutableClock,
) -> None:
    settings = get_settings().model_copy(
        update={
            "reconcile_backoff_base_seconds": 60.0,
            "reconcile_backoff_max_seconds": 600.0,
            "reconcile_max_checks": 3,
        }
    )
    await payments.set_fault(mode="drop_response", count=1)
    case = await approve_priya(client, auth_headers)
    await payments.set_fault(mode="error", count=100)  # every lookup now fails

    assert await reconcile(session, payments, settings, clock) == 1
    operation = await operation_for(session, case)
    assert operation.last_reconcile_error == "PROVIDER_ERROR"
    assert operation.next_reconcile_at is not None
    assert operation.next_reconcile_at >= clock.now + timedelta(seconds=30)  # backed off
    assert await reconcile(session, payments, settings, clock) == 0  # not due yet

    clock.advance(3600)
    await reconcile(session, payments, settings, clock)
    assert (await reload(session, WorkflowRun, case.run_id)).status == RunStatus.EXECUTING
    clock.advance(3600)
    await reconcile(session, payments, settings, clock)  # the third check: give up

    operation = await operation_for(session, case)
    assert operation.status == RefundOperationStatus.PENDING_RECONCILIATION  # untouched
    assert operation.next_reconcile_at is None
    assert operation.reconcile_checks == 3
    run = await reload(session, WorkflowRun, case.run_id)
    assert run.status == RunStatus.ESCALATED
    ticket = await reload(session, Ticket, case.ticket_id)
    assert ticket.status == TicketStatus.ESCALATED
    assert ticket.customer_reply == ESCALATION_MESSAGE
    escalated = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.run_id == case.run_id, AuditEvent.event_type == "run.escalated"
        )
    )
    assert escalated is not None
    assert escalated.redacted_payload["reason"] == reconciliation.EXHAUSTED
    assert await count_events(session, case.run_id, "refund.reconciliation_stopped") == 1
    assert await count_events(session, case.run_id, "refund.succeeded") == 0

    # Never checked again, and nothing was ever re-sent.
    clock.advance(86400)
    assert await reconcile(session, payments, settings, clock) == 0
    assert operation.resent_at is None
    assert await payments.refund_count() == 1  # only the original, dropped-reply refund


async def test_escalated_run_keeps_its_person_but_money_records_settle(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    settings: Settings,
    clock: MutableClock,
) -> None:
    await payments.set_fault(mode="drop_response", count=1)
    case = await approve_priya(client, auth_headers)
    # Its job died meanwhile (ADR-012): the case is already with a person.
    await escalate_after_dead_job(
        session,
        get_settings(),
        case.run_id,
        job_type=JobType.RESUME_AFTER_DECISION,
        code="X",
        now=clock.now,
    )
    await session.commit()

    clock.advance(10)
    await reconcile(session, payments, settings, clock)

    assert (await operation_for(session, case)).status == RefundOperationStatus.SUCCEEDED
    proposal = await reload(session, ActionProposal, case.proposal_id)
    assert proposal.status == ProposalStatus.EXECUTED
    item = await memory_for(session, case.run_id)
    assert item is not None and item.outcome_code == MemoryOutcome.REFUND_SUCCEEDED
    assert await count_events(session, case.run_id, "refund.reconciled") == 1
    # The run and the ticket stay with the person, with the escalation reply.
    assert (await reload(session, WorkflowRun, case.run_id)).status == RunStatus.ESCALATED
    ticket = await reload(session, Ticket, case.ticket_id)
    assert ticket.status == TicketStatus.ESCALATED
    assert ticket.customer_reply == ESCALATION_MESSAGE


# ---------------------------------------------------------------- two reconcilers


async def test_only_the_current_holder_can_settle_an_operation(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    settings: Settings,
    clock: MutableClock,
) -> None:
    await payments.set_fault(mode="drop_response", count=1)
    case = await approve_priya(client, auth_headers)
    clock.advance(10)
    lease = settings.reconcile_lease_seconds

    # Reconciler A claims and asks the provider...
    claim_a = await refunds.claim_for_reconciliation(session, now=clock.now, lease_seconds=lease)
    assert claim_a is not None
    # ...while A holds the lease, B finds nothing to claim.
    second = await refunds.claim_for_reconciliation(session, now=clock.now, lease_seconds=lease)
    assert second is None
    async with payments.client() as client_:
        finding_a = await reconciliation.check_claim(session, client_, claim_a, clock.now)

    # A stalls past its lease; B takes over and settles.
    clock.advance(lease + 1)
    claim_b = await refunds.claim_for_reconciliation(session, now=clock.now, lease_seconds=lease)
    assert claim_b is not None and claim_b.check == claim_a.check + 1
    async with payments.client() as client_:
        finding_b = await reconciliation.check_claim(session, client_, claim_b, clock.now)
    await reconciliation.settle_claim(session, settings, claim_b, finding_b, clock.now)

    # A wakes up and tries to settle with its stale claim: nothing is written.
    await reconciliation.settle_claim(session, settings, claim_a, finding_a, clock.now)

    await assert_resolved_with_success(session, case)
    assert await count_events(session, case.run_id, "refund.reconciled") == 1
    assert await count_events(session, case.run_id, "run.completed") == 1
    assert await payments.refund_count() == 1


async def test_stale_reconciler_cannot_resend_after_takeover(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    settings: Settings,
    clock: MutableClock,
) -> None:
    await payments.set_fault(mode="error", count=1)  # no refund at the provider
    case = await approve_priya(client, auth_headers)
    lease = settings.reconcile_lease_seconds

    claim_a = await refunds.claim_for_reconciliation(session, now=clock.now, lease_seconds=lease)
    assert claim_a is not None
    clock.advance(lease + 1)
    claim_b = await refunds.claim_for_reconciliation(session, now=clock.now, lease_seconds=lease)
    assert claim_b is not None

    # Both see "no refund with this key"; only the current holder may re-send.
    async with payments.client() as client_:
        finding_a = await reconciliation.check_claim(session, client_, claim_a, clock.now)
        assert not finding_a.resent and finding_a.error_code == "CLAIM_LOST"
        assert await payments.refund_count() == 0  # the stale reconciler sent nothing
        finding_b = await reconciliation.check_claim(session, client_, claim_b, clock.now)
    assert finding_b.resent
    await reconciliation.settle_claim(session, settings, claim_b, finding_b, clock.now)
    await reconciliation.settle_claim(session, settings, claim_a, finding_a, clock.now)

    assert await payments.refund_count() == 1
    assert await count_events(session, case.run_id, "refund.resent") == 1
    assert (await operation_for(session, case)).status == RefundOperationStatus.SUBMITTED


async def test_late_executor_cannot_overwrite_a_settled_operation(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    settings: Settings,
    clock: MutableClock,
) -> None:
    await payments.set_fault(mode="drop_response", count=1)
    case = await approve_priya(client, auth_headers)
    clock.advance(10)
    await reconcile(session, payments, settings, clock)
    operation = await operation_for(session, case)
    assert operation.status == RefundOperationStatus.SUCCEEDED

    # An executor that stalled writes its (older) answer: only a reserved row accepts it.
    written = await refunds.record_first_outcome(
        session,
        operation,
        RefundOperationStatus.PENDING_RECONCILIATION,
        None,
        next_check_at=clock.now,
    )
    assert not written
    assert (await operation_for(session, case)).status == RefundOperationStatus.SUCCEEDED


# ---------------------------------------------------------------- through the worker


async def test_worker_sweep_reconciles_refunds(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
    clock: MutableClock,
    drain_jobs: Callable[[], Awaitable[int]],
) -> None:
    await payments.set_fault(mode="drop_response", count=1)
    case = await approve_priya(client, auth_headers)
    clock.advance(10)

    scheduled = await queue.schedule_sweep(
        session, JobType.RECONCILE_REFUNDS, now=clock.now, max_attempts=3
    )
    assert scheduled
    assert await drain_jobs() == 1

    await assert_resolved_with_success(session, case)
