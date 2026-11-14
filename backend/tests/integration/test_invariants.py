"""The invariant checker (services/invariants.py) catches every broken rule it names.

A checker that never fails proves nothing, so each test starts from a sound world (Priya's
refund approved, paid out and verified) and breaks exactly one thing by hand, the way a
bug would. All changes are rolled back with the test session; the provider's table is
emptied by the next test's fixture.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from mock_providers.payments.app import create_app as create_payments_app
from mock_providers.payments.config import PaymentsSettings
from support_agent.api.deps import get_clock, get_payment_client
from support_agent.core.config import Settings, get_settings
from support_agent.db.models import (
    ActionProposal,
    Approval,
    Job,
    Order,
    RefundOperation,
    Ticket,
    WorkflowRun,
)
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import (
    ApprovalDecision,
    JobType,
    RefundOperationStatus,
    RunStatus,
    TicketStatus,
)
from support_agent.integrations.payments import PaymentClient
from support_agent.services import reconciliation
from support_agent.services.invariants import InvariantCode, Violation, check_money_invariants

AuthHeaders = Callable[[str], Awaitable[dict[str, str]]]
SEED_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.now = SEED_NOW

    def __call__(self) -> datetime:
        return self.now


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

    async def execute(self, sql: str, **params: object) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text(sql), params)


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


@dataclass(frozen=True)
class World:
    refund: RefundOperation
    run_id: UUID
    ticket_id: UUID
    order_id: UUID


@pytest.fixture
async def world(
    app: FastAPI,
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    clock: MutableClock,
    auth_headers: AuthHeaders,
    demo_password_hash: str,
    ingested_policies: None,
) -> World:
    """Priya's refund: approved (manual run), paid out and verified by the reconciler."""
    await seed(session, load_scenarios(), SEED_NOW, demo_password_hash)
    app.dependency_overrides[get_clock] = lambda: clock

    async def payment_client() -> AsyncIterator[PaymentClient]:
        async with payments.client() as provider:
            yield provider

    app.dependency_overrides[get_payment_client] = payment_client

    ticket = await client.post(
        "/tickets",
        json={"message": "Refund please", "order_number": "ORD-1001"},
        headers=await auth_headers("priya@example.com"),
    )
    reviewer = await auth_headers("anita@example.com")
    proposed = await client.post(
        f"/tickets/{ticket.json()['id']}/refund-proposals", headers=reviewer
    )
    body = proposed.json()
    decided = await client.post(
        f"/approvals/{body['approval_id']}/decision", json={"decision": "approve"}, headers=reviewer
    )
    assert decided.status_code == 200

    clock.now += timedelta(seconds=30)  # the provider finishes the refund
    settings = get_settings().model_copy(update={"reconcile_backoff_base_seconds": 0.0})
    async with payments.client() as provider:
        await reconciliation.reconcile_due_refunds(session, provider, settings, clock)

    refund = await session.scalar(
        select(RefundOperation).where(RefundOperation.proposal_id == UUID(body["proposal_id"]))
    )
    assert refund is not None and refund.status == RefundOperationStatus.SUCCEEDED
    return World(refund, UUID(body["run_id"]), UUID(body["ticket_id"]), refund.order_id)


async def violations(session: AsyncSession, grace_seconds: int = 0) -> list[Violation]:
    await session.flush()
    return await check_money_invariants(session, grace_seconds=grace_seconds)


async def codes(session: AsyncSession) -> set[InvariantCode]:
    return {violation.code for violation in await violations(session)}


async def set_row(session: AsyncSession, model: type, row_id: UUID, **values: object) -> None:
    """Change a row behind the services' backs, the way a bug would."""
    await session.execute(update(model).where(model.id == row_id).values(**values))  # type: ignore[attr-defined]


# ---------------------------------------------------------------- the baseline


async def test_a_sound_world_has_no_violations(session: AsyncSession, world: World) -> None:
    # It includes the seed's past refund, which the provider cannot know (made before it).
    assert await violations(session) == []


async def test_violations_carry_codes_and_ids_only(session: AsyncSession, world: World) -> None:
    await set_row(session, Ticket, world.ticket_id, status=TicketStatus.IN_PROGRESS)
    (violation,) = await violations(session)
    assert violation == Violation(
        InvariantCode.TICKET_BEHIND_REFUND, {"ticket_id": str(world.ticket_id)}
    )


# ---------------------------------------------------------------- amounts and approvals


async def test_refunds_above_the_paid_amount(session: AsyncSession, world: World) -> None:
    await set_row(session, Order, world.order_id, paid_amount_minor=world.refund.amount_minor - 1)
    found = await violations(session)
    assert Violation(InvariantCode.REFUNDS_EXCEED_PAID, {"order_id": str(world.order_id)}) in found


async def test_a_refund_whose_approval_was_rejected(session: AsyncSession, world: World) -> None:
    approval = await session.scalar(
        select(Approval).where(Approval.proposal_id == world.refund.proposal_id)
    )
    assert approval is not None
    await set_row(session, Approval, approval.id, decision=ApprovalDecision.REJECTED)
    assert await codes(session) == {InvariantCode.REFUND_NOT_APPROVED}


async def test_a_proposal_edited_after_approval(session: AsyncSession, world: World) -> None:
    proposal = await session.get(ActionProposal, world.refund.proposal_id)
    assert proposal is not None
    edited = {**proposal.payload, "amount_minor": proposal.payload["amount_minor"] + 100}
    await set_row(session, ActionProposal, proposal.id, payload=edited)
    # The stored hash no longer matches the payload, and the refund no longer matches it.
    assert await codes(session) == {
        InvariantCode.REFUND_NOT_APPROVED,
        InvariantCode.REFUND_DIFFERS_FROM_PROPOSAL,
    }


async def test_a_refund_for_another_amount_than_approved(
    session: AsyncSession, world: World
) -> None:
    await set_row(session, RefundOperation, world.refund.id, amount_minor=100)
    assert await codes(session) == {
        InvariantCode.REFUND_DIFFERS_FROM_PROPOSAL,
        InvariantCode.PROVIDER_AMOUNT_MISMATCH,
    }


async def test_two_refunds_for_one_proposal(session: AsyncSession, world: World) -> None:
    # The unique constraint forbids it, so drop it inside this rolled-back transaction: the
    # checker must not depend on the database's constraints to see the problem.
    constraint = await session.scalar(
        text(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'refund_operations'::regclass "
            "AND contype = 'u' AND pg_get_constraintdef(oid) LIKE '%(proposal_id)%'"
        )
    )
    await session.execute(text(f'ALTER TABLE refund_operations DROP CONSTRAINT "{constraint}"'))
    session.add(
        RefundOperation(
            order_id=world.order_id,
            proposal_id=world.refund.proposal_id,
            amount_minor=1,
            idempotency_key="refund-duplicate",
            status=RefundOperationStatus.FAILED,
        )
    )
    found = await violations(session)
    assert (
        Violation(
            InvariantCode.DUPLICATE_REFUND_FOR_PROPOSAL,
            {"proposal_id": str(world.refund.proposal_id)},
        )
        in found
    )


# ---------------------------------------------------------------- agreement with the provider


async def test_a_refund_the_provider_does_not_hold(
    session: AsyncSession, world: World, payments: Payments
) -> None:
    await payments.execute("DELETE FROM mock_payments.refunds")
    assert await codes(session) == {
        InvariantCode.PROVIDER_REFUND_MISSING,
        InvariantCode.TICKET_RESOLVED_WITHOUT_REFUND,  # nothing at the provider confirms it
    }


async def test_a_provider_refund_we_never_asked_for(
    session: AsyncSession, world: World, payments: Payments
) -> None:
    await payments.execute(
        "INSERT INTO mock_payments.refunds VALUES "
        "('re_orphan', 'someone-elses-key', 'ORD-1001', 100, 'INR', 'x', 'succeeded', now())"
    )
    found = await violations(session)
    assert found == [
        Violation(InvariantCode.PROVIDER_REFUND_ORPHAN, {"provider_reference": "re_orphan"})
    ]


async def test_provider_amount_reference_and_outcome_must_agree(
    session: AsyncSession, world: World, payments: Payments
) -> None:
    await payments.execute(
        "UPDATE mock_payments.refunds SET amount_minor = amount_minor + 1, final_outcome = 'failed'"
    )
    await set_row(session, RefundOperation, world.refund.id, provider_reference="re_wrong")
    assert await codes(session) == {
        InvariantCode.PROVIDER_AMOUNT_MISMATCH,
        InvariantCode.PROVIDER_REFERENCE_MISMATCH,
        InvariantCode.PROVIDER_OUTCOME_MISMATCH,
        InvariantCode.TICKET_RESOLVED_WITHOUT_REFUND,
    }


async def test_we_say_failed_while_the_provider_pays_out(
    session: AsyncSession, world: World
) -> None:
    await set_row(session, RefundOperation, world.refund.id, status=RefundOperationStatus.FAILED)
    assert InvariantCode.PROVIDER_OUTCOME_MISMATCH in await codes(session)


# ---------------------------------------------------------------- tickets


async def test_a_ticket_resolved_before_the_money_arrived(
    session: AsyncSession, world: World, clock: MutableClock
) -> None:
    await set_row(
        session,
        RefundOperation,
        world.refund.id,
        status=RefundOperationStatus.PROCESSING,
        next_reconcile_at=clock(),
    )
    assert await codes(session) == {InvariantCode.TICKET_RESOLVED_WITHOUT_REFUND}


# ---------------------------------------------------------------- nothing left unwatched


async def test_an_unsettled_refund_nothing_will_check(session: AsyncSession, world: World) -> None:
    await set_row(
        session,
        RefundOperation,
        world.refund.id,
        status=RefundOperationStatus.PENDING_RECONCILIATION,
        next_reconcile_at=None,
    )
    await set_row(session, Ticket, world.ticket_id, status=TicketStatus.IN_PROGRESS)
    assert await codes(session) == {InvariantCode.REFUND_UNWATCHED}

    # Handed to a person (the reconciler gave up and escalated): that is allowed.
    await set_row(session, WorkflowRun, world.run_id, status=RunStatus.ESCALATED)
    assert await codes(session) == set()


async def test_a_run_stuck_executing(
    session: AsyncSession, world: World, clock: MutableClock
) -> None:
    await set_row(session, WorkflowRun, world.run_id, status=RunStatus.EXECUTING)
    assert await codes(session) == {InvariantCode.RUN_STUCK_EXECUTING}

    # An open job for the run will move it on: not stuck.
    session.add(Job(job_type=JobType.RESUME_AFTER_DECISION, run_id=world.run_id))
    assert await codes(session) == set()


async def test_the_grace_period_spares_work_in_flight(session: AsyncSession, world: World) -> None:
    await set_row(session, WorkflowRun, world.run_id, status=RunStatus.EXECUTING)
    await set_row(session, Ticket, world.ticket_id, status=TicketStatus.IN_PROGRESS)
    assert {v.code for v in await violations(session, grace_seconds=0)} == {
        InvariantCode.RUN_STUCK_EXECUTING,
        InvariantCode.TICKET_BEHIND_REFUND,
    }
    # Changed just now (this transaction): on a live system that is still in flight.
    assert await violations(session, grace_seconds=600) == []
