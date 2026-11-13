"""reset_demo removes what demos and runs created and restores the seeded scenarios.

Every check reads database state, not the counts the function reports.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mock_providers.payments.storage import SCHEMA as PAYMENTS_SCHEMA
from mock_providers.payments.storage import metadata as payments_metadata
from mock_providers.ticketing.storage import SCHEMA as TICKETING_SCHEMA
from mock_providers.ticketing.storage import metadata as ticketing_metadata
from support_agent.core.config import get_settings
from support_agent.core.security import hash_password
from support_agent.db.base import Base
from support_agent.db.demo_reset import reset_demo
from support_agent.db.models import (
    ActionProposal,
    Approval,
    AuditEvent,
    Customer,
    Order,
    RefundOperation,
    Return,
    Ticket,
    WorkflowRun,
)
from support_agent.db.seed import ResetRefusedError, SeedData, load_scenarios, seed
from support_agent.domain.enums import (
    ActionType,
    ApprovalDecision,
    InspectionStatus,
    ProposalStatus,
    RefundOperationStatus,
    ReturnStatus,
    RunStatus,
    TicketCategory,
    TicketStatus,
)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PASSWORD_HASH = hash_password("demo-pass-for-tests")
PROPOSAL_HASH = "a" * 64


@pytest.fixture(scope="module")
def data() -> SeedData:
    return load_scenarios()


async def count(session: AsyncSession, model: type[Base]) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def order_by_number(session: AsyncSession, order_number: str) -> Order:
    order = await session.scalar(select(Order).where(Order.order_number == order_number))
    assert order is not None
    return order


async def add_demo_run(session: AsyncSession, order: Order) -> None:
    """What one demo leaves behind: ticket -> run -> proposal -> approval -> refund."""
    ticket = Ticket(
        customer_id=order.customer_id,
        order_id=order.id,
        category=TicketCategory.REFUND,
        status=TicketStatus.OPEN,
        summary="Demo run",
    )
    session.add(ticket)
    await session.flush()
    run = WorkflowRun(ticket_id=ticket.id, thread_id="demo-thread", status=RunStatus.EXECUTING)
    session.add(run)
    await session.flush()
    proposal = ActionProposal(
        run_id=run.id,
        action_type=ActionType.REQUEST_REFUND_APPROVAL,
        payload={"order_number": order.order_number},
        payload_hash=PROPOSAL_HASH,
        status=ProposalStatus.APPROVED,
    )
    session.add(proposal)
    await session.flush()
    session.add_all(
        [
            Approval(
                proposal_id=proposal.id,
                payload_hash=PROPOSAL_HASH,
                reviewer_id="demo-reviewer",
                decision=ApprovalDecision.APPROVED,
                created_at=NOW,
                expires_at=NOW + timedelta(hours=2),
                decided_at=NOW + timedelta(hours=1),
            ),
            RefundOperation(
                order_id=order.id,
                proposal_id=proposal.id,
                amount_minor=1_000,
                idempotency_key="demo-refund-key",
                status=RefundOperationStatus.RESERVED,
            ),
            AuditEvent(run_id=run.id, actor="system", event_type="demo.run"),
            AuditEvent(run_id=None, actor="reviewer", event_type="demo.queue_opened"),
        ]
    )
    await session.flush()


async def add_mock_provider_rows(session: AsyncSession) -> None:
    """Create the mock providers' tables (rolled back with the test) and put a row in each."""
    connection = await session.connection()
    for schema, metadata in (
        (PAYMENTS_SCHEMA, payments_metadata),
        (TICKETING_SCHEMA, ticketing_metadata),
    ):
        await connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await connection.run_sync(metadata.create_all)
    await session.execute(
        text(
            "INSERT INTO mock_payments.refunds (provider_reference, idempotency_key, "
            "order_reference, amount_minor, currency, reason, final_outcome, created_at) "
            "VALUES ('mockpay_rf_demo', 'demo-key', 'ORD-1001', 1000, 'INR', 'demo', "
            "'succeeded', now())"
        )
    )
    await session.execute(
        text(
            "INSERT INTO mock_ticketing.tickets (id, idempotency_key, customer_reference, "
            "subject, description, status, created_at, updated_at) "
            "VALUES ('tkt_demo', 'demo-tkt', 'cust', 'Demo', 'Demo', 'open', now(), now())"
        )
    )
    await session.execute(
        text(
            "INSERT INTO mock_ticketing.notes (id, ticket_id, idempotency_key, body, created_at) "
            "VALUES ('note_demo', 'tkt_demo', 'demo-note', 'Demo', now())"
        )
    )


async def mock_row_counts(session: AsyncSession) -> list[int]:
    queries = (
        "mock_payments.refunds",
        "mock_ticketing.tickets",
        "mock_ticketing.notes",
    )
    return [
        int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608
        for table in queries
    ]


async def test_reset_removes_demo_data_and_mock_provider_records(
    session: AsyncSession, data: SeedData
) -> None:
    await seed(session, data, NOW, PASSWORD_HASH)
    await add_demo_run(session, await order_by_number(session, "ORD-1001"))
    await add_mock_provider_rows(session)
    # (other mock tests may have left rows in this database, so only check there are some)
    assert all(n >= 1 for n in await mock_row_counts(session))

    await reset_demo(session, data, NOW, PASSWORD_HASH)

    # Only Kavya's seeded refund history remains: one ticket, run, proposal, approval, refund.
    for model in (Ticket, WorkflowRun, ActionProposal, Approval, RefundOperation):
        assert await count(session, model) == 1, model.__name__
    kavya_order = await order_by_number(session, "ORD-1005")
    refund_orders = list(await session.scalars(select(RefundOperation.order_id)))
    assert refund_orders == [kavya_order.id]
    assert await count(session, AuditEvent) == 0
    assert await mock_row_counts(session) == [0, 0, 0]


async def test_reset_restores_scenarios_that_a_demo_changed(
    session: AsyncSession, data: SeedData
) -> None:
    await seed(session, data, NOW, PASSWORD_HASH)
    priya = await order_by_number(session, "ORD-1001")
    priya.delivered_at = NOW - timedelta(days=90)  # a demo (or a bug) made her ineligible
    await session.execute(
        Return.__table__.update()  # type: ignore[attr-defined]
        .where(Return.order_id == priya.id)
        .values(status=ReturnStatus.REQUESTED, inspection_status=InspectionStatus.FAILED)
    )
    await add_demo_run(session, priya)
    await session.flush()

    anchor = NOW + timedelta(days=1)  # the scenarios are rebuilt from the anchor given
    await reset_demo(session, data, anchor, PASSWORD_HASH)

    session.expire_all()
    priya = await order_by_number(session, "ORD-1001")
    assert priya.delivered_at == anchor - timedelta(days=12)
    assert priya.paid_amount_minor == 249_900
    [priya_return] = list(await session.scalars(select(Return).where(Return.order_id == priya.id)))
    assert priya_return.status == ReturnStatus.RECEIVED
    assert priya_return.inspection_status == InspectionStatus.PASSED
    assert priya_return.received_at == anchor - timedelta(days=5)
    priya_refunds = await session.scalar(
        select(func.count()).where(RefundOperation.order_id == priya.id)
    )
    assert priya_refunds == 0  # her full balance is refundable again
    assert await count(session, Customer) == len(data.customers)


async def test_reset_twice_gives_the_same_state(session: AsyncSession, data: SeedData) -> None:
    await seed(session, data, NOW, PASSWORD_HASH)
    await reset_demo(session, data, NOW, PASSWORD_HASH)
    first = [await count(session, m) for m in (Customer, Order, Return, RefundOperation)]

    await reset_demo(session, data, NOW, PASSWORD_HASH)

    assert [await count(session, m) for m in (Customer, Order, Return, RefundOperation)] == first


@pytest.fixture
def production_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()  # settings are cached
    yield
    get_settings.cache_clear()


async def test_reset_demo_refuses_to_run_in_production(
    session: AsyncSession, data: SeedData, production_env: None
) -> None:
    await seed(session, data, NOW, PASSWORD_HASH)
    await add_demo_run(session, await order_by_number(session, "ORD-1001"))

    with pytest.raises(ResetRefusedError, match="production"):
        await reset_demo(session, data, NOW, PASSWORD_HASH)

    assert await count(session, WorkflowRun) == 2  # the demo run and Kavya's history
    assert await count(session, AuditEvent) == 2
