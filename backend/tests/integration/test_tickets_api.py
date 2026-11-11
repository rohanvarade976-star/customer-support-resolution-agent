"""POST /tickets, GET /tickets and GET /tickets/{id}: ownership, roles and pagination."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import Ticket
from support_agent.db.seed import load_scenarios, seed

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

Headers = dict[str, str]


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


def assert_error(response: httpx.Response, status: int, code: str) -> None:
    body: dict[str, Any] = response.json()
    assert response.status_code == status
    assert body["success"] is False
    assert body["error_code"] == code


async def create(
    client: httpx.AsyncClient, headers: Headers, message: str, order_number: str | None = None
) -> httpx.Response:
    body: dict[str, Any] = {"message": message}
    if order_number is not None:
        body["order_number"] = order_number
    return await client.post("/tickets", json=body, headers=headers)


async def ticket_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Ticket)) or 0


# ---------------------------------------------------------------- create


async def test_customer_creates_a_ticket_on_their_own_order(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    headers = await auth_headers("priya@example.com")

    response = await create(client, headers, "My headphones are broken", "ORD-1001")

    assert response.status_code == 201
    body = response.json()
    assert body["order_number"] == "ORD-1001"
    assert body["summary"] == "My headphones are broken"
    assert body["status"] == "open"
    assert body["category"] is None  # the classifier sets it later
    assert "customer_id" not in body  # customers don't get the reviewer fields
    stored = await session.get(Ticket, body["id"])
    assert stored is not None
    assert stored.order_id is not None


async def test_ticket_without_an_order_is_allowed(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    headers = await auth_headers("priya@example.com")

    response = await create(client, headers, "General question about delivery times")

    assert response.status_code == 201
    assert response.json()["order_number"] is None


async def test_rahul_cannot_create_a_ticket_on_priyas_order(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    headers = await auth_headers("rahul@example.com")
    before = await ticket_count(session)

    response = await create(client, headers, "Refund ORD-1001 please", "ORD-1001")

    assert_error(response, 404, "ORDER_NOT_FOUND")
    assert await ticket_count(session) == before  # no row was created


async def test_ticket_body_cannot_name_a_customer(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    headers = await auth_headers("rahul@example.com")
    before = await ticket_count(session)

    response = await client.post(
        "/tickets", json={"message": "hi", "customer_id": str(uuid4())}, headers=headers
    )

    assert_error(response, 422, "VALIDATION_ERROR")
    assert await ticket_count(session) == before


@pytest.mark.parametrize(
    "body",
    [
        {"message": ""},
        {"message": "   "},
        {"message": "x" * 2001},
        {"message": "ok", "order_number": "1001"},
        {},
    ],
)
async def test_invalid_ticket_bodies_are_rejected(
    client: httpx.AsyncClient,
    session: AsyncSession,
    body: dict[str, Any],
    auth_headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    headers = await auth_headers("priya@example.com")
    before = await ticket_count(session)

    response = await client.post("/tickets", json=body, headers=headers)

    assert_error(response, 422, "VALIDATION_ERROR")
    assert await ticket_count(session) == before


async def test_reviewer_cannot_create_a_ticket(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    headers = await auth_headers("anita@example.com")
    before = await ticket_count(session)

    response = await create(client, headers, "hello")

    assert_error(response, 403, "FORBIDDEN")
    assert await ticket_count(session) == before


async def test_create_ticket_without_a_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.post("/tickets", json={"message": "hello"})

    assert_error(response, 401, "NOT_AUTHENTICATED")


# ---------------------------------------------------------------- list


async def test_customers_list_only_their_own_tickets(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    priya = await auth_headers("priya@example.com")
    rahul = await auth_headers("rahul@example.com")
    await create(client, priya, "Priya's problem")
    await create(client, rahul, "Rahul's problem")

    priya_list = (await client.get("/tickets", headers=priya)).json()
    rahul_list = (await client.get("/tickets", headers=rahul)).json()

    assert [t["summary"] for t in priya_list["items"]] == ["Priya's problem"]
    assert [t["summary"] for t in rahul_list["items"]] == ["Rahul's problem"]
    assert "customer_name" not in priya_list["items"][0]


async def test_reviewer_sees_all_tickets_with_customer_details(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    priya = await auth_headers("priya@example.com")
    rahul = await auth_headers("rahul@example.com")
    await create(client, priya, "Priya's problem", "ORD-1001")
    await create(client, rahul, "Rahul's problem")
    anita = await auth_headers("anita@example.com")

    response = await client.get("/tickets", headers=anita)

    items = {t["summary"]: t for t in response.json()["items"]}
    assert {"Priya's problem", "Rahul's problem"} <= set(items)
    assert items["Priya's problem"]["customer_name"] == "Priya Sharma"
    assert items["Priya's problem"]["order_number"] == "ORD-1001"
    assert items["Rahul's problem"]["customer_name"] == "Rahul Verma"
    assert items["Rahul's problem"]["customer_id"]


async def test_ticket_list_is_paginated_newest_first(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    priya = await auth_headers("priya@example.com")
    for number in range(3):
        await create(client, priya, f"ticket {number}")

    first = (await client.get("/tickets?limit=2", headers=priya)).json()
    second = (await client.get("/tickets?limit=2&offset=2", headers=priya)).json()

    assert first["limit"] == 2
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    ids = [t["id"] for t in first["items"] + second["items"]]
    assert len(set(ids)) == 3  # no overlap between pages


@pytest.mark.parametrize("query", ["limit=101", "limit=0", "offset=-1", "limit=abc"])
async def test_ticket_list_rejects_bad_pagination(
    client: httpx.AsyncClient, query: str, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    anita = await auth_headers("anita@example.com")

    response = await client.get(f"/tickets?{query}", headers=anita)

    assert_error(response, 422, "VALIDATION_ERROR")


async def test_ticket_list_accepts_the_maximum_limit(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    anita = await auth_headers("anita@example.com")

    response = await client.get("/tickets?limit=100", headers=anita)

    assert response.status_code == 200


async def test_ticket_list_without_a_token_is_401(client: httpx.AsyncClient) -> None:
    assert_error(await client.get("/tickets"), 401, "NOT_AUTHENTICATED")


# ---------------------------------------------------------------- get one


async def test_owner_and_reviewer_can_get_a_ticket(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    priya = await auth_headers("priya@example.com")
    created = (await create(client, priya, "Broken", "ORD-1001")).json()
    anita = await auth_headers("anita@example.com")

    as_owner = await client.get(f"/tickets/{created['id']}", headers=priya)
    as_reviewer = await client.get(f"/tickets/{created['id']}", headers=anita)

    assert as_owner.status_code == 200
    assert as_owner.json() == created
    assert as_reviewer.status_code == 200
    assert as_reviewer.json()["customer_name"] == "Priya Sharma"


async def test_customer_gets_404_for_another_customers_ticket(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    priya = await auth_headers("priya@example.com")
    rahul = await auth_headers("rahul@example.com")
    created = (await create(client, priya, "Private")).json()

    someone_elses = await client.get(f"/tickets/{created['id']}", headers=rahul)
    missing = await client.get(f"/tickets/{uuid4()}", headers=rahul)

    assert_error(someone_elses, 404, "TICKET_NOT_FOUND")
    assert someone_elses.json() == missing.json()  # existence is not revealed


async def test_get_ticket_without_a_token_is_401(client: httpx.AsyncClient) -> None:
    assert_error(await client.get(f"/tickets/{uuid4()}"), 401, "NOT_AUTHENTICATED")
