"""GET /me/orders and /me/orders/{order_number}: ownership, shape and query count."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import Customer, Order, OrderItem
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import OrderStatus

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


def assert_error(response: httpx.Response, status: int, code: str) -> None:
    body: dict[str, Any] = response.json()
    assert response.status_code == status
    assert body["success"] is False
    assert body["error_code"] == code


async def test_priya_sees_only_her_own_orders(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    response = await client.get("/me/orders", headers=await auth_headers("priya@example.com"))

    assert response.status_code == 200
    orders = response.json()
    assert [order["order_number"] for order in orders] == ["ORD-1001"]
    order = orders[0]
    assert order["paid_amount_minor"] == 249900
    assert order["items"][0]["product_name"] == "Wireless Headphones"
    assert order["returns"][0]["status"] == "received"


async def test_order_response_has_no_internal_fields(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    response = await client.get(
        "/me/orders/ORD-1001", headers=await auth_headers("priya@example.com")
    )

    order = response.json()
    assert set(order) == {
        "order_number",
        "status",
        "currency",
        "paid_amount_minor",
        "placed_at",
        "delivered_at",
        "items",
        "returns",
    }
    assert set(order["items"][0]) == {
        "product_id",
        "product_name",
        "quantity",
        "paid_amount_minor",
    }
    assert set(order["returns"][0]) == {"status", "requested_at", "received_at"}


async def test_customer_can_get_their_own_order(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    response = await client.get(
        "/me/orders/ORD-1001", headers=await auth_headers("priya@example.com")
    )

    assert response.status_code == 200
    assert response.json()["order_number"] == "ORD-1001"


async def test_rahul_gets_404_for_priyas_order(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    headers = await auth_headers("rahul@example.com")

    other_customers = await client.get("/me/orders/ORD-1001", headers=headers)
    missing = await client.get("/me/orders/ORD-9999", headers=headers)

    assert_error(other_customers, 404, "ORDER_NOT_FOUND")
    # Identical to an order that does not exist, so existence is not revealed.
    assert other_customers.json() == missing.json()


async def test_orders_without_a_token_is_401(client: httpx.AsyncClient) -> None:
    assert_error(await client.get("/me/orders"), 401, "NOT_AUTHENTICATED")
    assert_error(await client.get("/me/orders/ORD-1001"), 401, "NOT_AUTHENTICATED")


async def test_reviewer_gets_403_on_customer_only_order_routes(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    headers = await auth_headers("anita@example.com")

    assert_error(await client.get("/me/orders", headers=headers), 403, "FORBIDDEN")
    assert_error(await client.get("/me/orders/ORD-1001", headers=headers), 403, "FORBIDDEN")


@contextmanager
def count_statements(session: AsyncSession) -> Iterator[list[str]]:
    """Collect every SQL statement the database receives while the block runs."""
    assert session.bind is not None
    engine = session.bind.sync_engine
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def test_order_list_uses_a_bounded_number_of_queries(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    headers = await auth_headers("priya@example.com")
    with count_statements(session) as before:
        await client.get("/me/orders", headers=headers)

    priya = await session.scalar(select(Customer).where(Customer.email == "priya@example.com"))
    assert priya is not None
    for number in range(2001, 2008):  # seven more orders, each with two items
        order = Order(
            order_number=f"ORD-{number}",
            customer_id=priya.id,
            status=OrderStatus.PLACED,
            paid_amount_minor=1000,
        )
        session.add(order)
        await session.flush()
        for sku in ("A", "B"):
            session.add(
                OrderItem(
                    order_id=order.id,
                    product_id=f"SKU-{sku}",
                    product_name=f"Item {sku}",
                    quantity=1,
                    paid_amount_minor=500,
                )
            )
    await session.flush()

    with count_statements(session) as after:
        response = await client.get("/me/orders", headers=headers)

    assert len(response.json()) == 8
    # Same number of queries for 1 order as for 8: no query per order (no N+1).
    assert len(after) == len(before)
    # 1 to check the token + orders + items + returns.
    assert len(after) <= 4
