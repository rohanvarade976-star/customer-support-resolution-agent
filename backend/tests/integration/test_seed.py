"""The seed creates each demo scenario exactly as described in scenarios.json.

Every check reads database state, not the counts the seed function reports.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import get_settings
from support_agent.db.base import Base
from support_agent.db.models import (
    ActionProposal,
    Approval,
    Customer,
    Order,
    RefundOperation,
    Return,
)
from support_agent.db.seed import (
    SEED_TABLES,
    ResetRefusedError,
    SeedData,
    load_scenarios,
    reset,
    seed,
)
from support_agent.domain.enums import InspectionStatus, RefundOperationStatus, ReturnStatus

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def data() -> SeedData:
    return load_scenarios()


async def count_rows(session: AsyncSession, model: type[Base]) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def table_counts(session: AsyncSession) -> dict[str, int]:
    return {model.__tablename__: await count_rows(session, model) for model in SEED_TABLES}


async def returns_for(session: AsyncSession, order_number: str) -> list[Return]:
    rows = await session.scalars(
        select(Return)
        .join(Order, Return.order_id == Order.id)
        .where(Order.order_number == order_number)
    )
    return list(rows)


async def test_seeding_twice_creates_nothing_new(session: AsyncSession, data: SeedData) -> None:
    await seed(session, data, NOW)
    after_first = await table_counts(session)

    second = await seed(session, data, NOW)

    assert await table_counts(session) == after_first
    assert sum(second.values()) == 0
    assert after_first["customers"] == len(data.customers)
    assert after_first["orders"] == sum(len(c.orders) for c in data.customers)


async def test_priya_return_received_and_passed_with_no_refund(
    session: AsyncSession, data: SeedData
) -> None:
    await seed(session, data, NOW)

    [priya_return] = await returns_for(session, "ORD-1001")
    assert priya_return.status == ReturnStatus.RECEIVED
    assert priya_return.inspection_status == InspectionStatus.PASSED
    assert priya_return.received_at == NOW - timedelta(days=5)

    order = await session.scalar(select(Order).where(Order.order_number == "ORD-1001"))
    assert order is not None
    assert order.paid_amount_minor == 249_900  # ₹2,499.00
    assert order.delivered_at == NOW - timedelta(days=12)
    refunds = await session.scalar(select(func.count()).where(RefundOperation.order_id == order.id))
    assert refunds == 0


async def test_kavya_fully_refunded_through_a_consistent_chain(
    session: AsyncSession, data: SeedData
) -> None:
    await seed(session, data, NOW)

    order = await session.scalar(select(Order).where(Order.order_number == "ORD-1005"))
    assert order is not None
    [refund] = list(
        await session.scalars(select(RefundOperation).where(RefundOperation.order_id == order.id))
    )
    assert refund.status == RefundOperationStatus.SUCCEEDED
    assert refund.amount_minor == order.paid_amount_minor

    proposal = await session.get(ActionProposal, refund.proposal_id)
    assert proposal is not None
    assert proposal.payload["amount_minor"] == order.paid_amount_minor
    assert proposal.payload["order_id"] == str(order.id)
    approval = await session.scalar(select(Approval).where(Approval.proposal_id == proposal.id))
    assert approval is not None
    assert approval.payload_hash == proposal.payload_hash


async def test_aditya_return_not_yet_received(session: AsyncSession, data: SeedData) -> None:
    await seed(session, data, NOW)

    [aditya_return] = await returns_for(session, "ORD-1007")
    assert aditya_return.status == ReturnStatus.REQUESTED
    assert aditya_return.received_at is None
    assert aditya_return.inspection_status == InspectionStatus.PENDING
    assert aditya_return.created_at == NOW - timedelta(days=2)


async def test_reset_deletes_every_seeded_row(session: AsyncSession, data: SeedData) -> None:
    await seed(session, data, NOW)

    await reset(session, data)

    assert all(count == 0 for count in (await table_counts(session)).values())


@pytest.fixture
def production_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Settings are cached, so clear the cache before and after changing APP_ENV.
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_reset_refuses_to_run_in_production(
    session: AsyncSession, data: SeedData, production_env: None
) -> None:
    await seed(session, data, NOW)
    before = await table_counts(session)

    with pytest.raises(ResetRefusedError, match="production"):
        await reset(session, data)

    assert await table_counts(session) == before
    assert await count_rows(session, Customer) == len(data.customers)
