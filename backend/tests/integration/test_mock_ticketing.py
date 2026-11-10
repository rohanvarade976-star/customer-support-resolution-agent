"""Integration tests for the mock ticketing provider (real Postgres, no server process).

Idempotency checks assert database rows, not just response text: "exactly one ticket"
or "exactly one note" is counted in the mock_ticketing tables.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from mock_providers.ticketing.app import create_app
from mock_providers.ticketing.config import TicketingSettings

TICKET: dict[str, Any] = {
    "customer_reference": "cust_arjun",
    "order_reference": "ORD-1002",
    "subject": "Laptop stuck in transit",
    "description": "My laptop has shown 'in transit' for 8 days.",
}
NOTE = {"body": "Shipment is held at the Nagpur hub; delay policy applies."}


@dataclass
class MockTicketing:
    client: httpx.AsyncClient
    engine: AsyncEngine

    async def create_ticket(
        self, key: str | None, payload: dict[str, Any] | None = None
    ) -> httpx.Response:
        headers = {"Idempotency-Key": key} if key else {}
        return await self.client.post("/tickets", json=payload or TICKET, headers=headers)

    async def new_ticket_id(self, key: str) -> str:
        response = await self.create_ticket(key)
        assert response.status_code == 201
        return str(response.json()["id"])

    async def add_note(
        self, ticket_id: str, key: str | None, payload: dict[str, Any] | None = None
    ) -> httpx.Response:
        headers = {"Idempotency-Key": key} if key else {}
        return await self.client.post(
            f"/tickets/{ticket_id}/notes", json=payload or NOTE, headers=headers
        )

    async def set_fault(self, **config: Any) -> None:
        response = await self.client.post("/_admin/faults", json=config)
        assert response.status_code == 200

    async def count(self, table: str, key: str) -> int:
        # table is a fixed name from this file, never user input.
        sql = f"SELECT count(*) FROM mock_ticketing.{table} WHERE idempotency_key = :k"  # noqa: S608
        async with self.engine.connect() as conn:
            result = await conn.scalar(text(sql), {"k": key})
        return int(result or 0)

    async def ticket_status(self, ticket_id: str) -> str:
        async with self.engine.connect() as conn:
            result = await conn.scalar(
                text("SELECT status FROM mock_ticketing.tickets WHERE id = :id"), {"id": ticket_id}
            )
        return str(result)


def make_settings(database_url: str, app_env: str = "test") -> TicketingSettings:
    return TicketingSettings(
        app_env=app_env,  # type: ignore[arg-type]  # Literal checked by pydantic at runtime
        database_url=SecretStr(database_url),
    )


@pytest.fixture
async def provider(test_database_url: str) -> AsyncIterator[MockTicketing]:
    app = create_app(make_settings(test_database_url))
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    # ASGITransport does not run startup/shutdown, so run the lifespan ourselves.
    async with app.router.lifespan_context(app):
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE mock_ticketing.notes, mock_ticketing.tickets"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
            yield MockTicketing(client=client, engine=engine)
    await engine.dispose()


# ---------------------------------------------------------------- ticket creation


async def test_create_ticket_returns_201_open(provider: MockTicketing) -> None:
    response = await provider.create_ticket("tkey-create")

    assert response.status_code == 201
    body = response.json()
    assert body["id"].startswith("tkt_")
    assert body["status"] == "open"
    assert body["order_reference"] == "ORD-1002"
    assert body["notes"] == []
    assert await provider.count("tickets", "tkey-create") == 1


async def test_ticket_replay_returns_original(provider: MockTicketing) -> None:
    first = await provider.create_ticket("tkey-replay")
    second = await provider.create_ticket("tkey-replay")

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert await provider.count("tickets", "tkey-replay") == 1


async def test_ticket_key_with_changed_body_returns_409(provider: MockTicketing) -> None:
    first = await provider.create_ticket("tkey-conflict")

    response = await provider.create_ticket("tkey-conflict", {**TICKET, "subject": "Other"})

    assert response.status_code == 409
    assert response.json()["error_code"] == "IDEMPOTENCY_KEY_REUSED"
    assert response.json()["retryable"] is False
    assert await provider.count("tickets", "tkey-conflict") == 1
    original = await provider.client.get(f"/tickets/{first.json()['id']}")
    assert original.json()["subject"] == TICKET["subject"]


async def test_ticket_without_key_returns_400(provider: MockTicketing) -> None:
    response = await provider.create_ticket(None)

    assert response.status_code == 400
    assert response.json()["error_code"] == "IDEMPOTENCY_KEY_MISSING"
    async with provider.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM mock_ticketing.tickets")) == 0


async def test_drop_response_on_ticket_creation_still_creates_one(
    provider: MockTicketing,
) -> None:
    await provider.set_fault(mode="drop_response", count=1)

    dropped = await provider.create_ticket("tkey-dropped")

    assert dropped.status_code == 504
    assert dropped.json()["error_code"] == "GATEWAY_TIMEOUT"
    assert await provider.count("tickets", "tkey-dropped") == 1
    # A safe retry with the same key returns that ticket instead of making a second one.
    retry = await provider.create_ticket("tkey-dropped")
    assert retry.status_code == 200
    assert await provider.count("tickets", "tkey-dropped") == 1


# ---------------------------------------------------------------- get and update


async def test_get_ticket_includes_notes_oldest_first(provider: MockTicketing) -> None:
    ticket_id = await provider.new_ticket_id("tkey-get")
    await provider.add_note(ticket_id, "nkey-get-1", {"body": "first"})
    await provider.add_note(ticket_id, "nkey-get-2", {"body": "second"})

    response = await provider.client.get(f"/tickets/{ticket_id}")

    assert response.status_code == 200
    assert [note["body"] for note in response.json()["notes"]] == ["first", "second"]


async def test_unknown_ticket_returns_404(provider: MockTicketing) -> None:
    get_response = await provider.client.get("/tickets/tkt_missing")
    patch_response = await provider.client.patch(
        "/tickets/tkt_missing", json={"status": "resolved"}
    )
    note_response = await provider.add_note("tkt_missing", "nkey-missing")

    for response in (get_response, patch_response, note_response):
        assert response.status_code == 404
        assert response.json()["error_code"] == "TICKET_NOT_FOUND"
    assert await provider.count("notes", "nkey-missing") == 0


async def test_patch_moves_through_every_status(provider: MockTicketing) -> None:
    ticket_id = await provider.new_ticket_id("tkey-patch")

    for status in ("pending", "resolved", "closed", "open"):
        response = await provider.client.patch(f"/tickets/{ticket_id}", json={"status": status})
        assert response.status_code == 200
        assert response.json()["status"] == status
        assert await provider.ticket_status(ticket_id) == status


async def test_patch_rejects_unknown_status(provider: MockTicketing) -> None:
    ticket_id = await provider.new_ticket_id("tkey-bad-status")

    response = await provider.client.patch(f"/tickets/{ticket_id}", json={"status": "done"})

    assert response.status_code == 422
    assert await provider.ticket_status(ticket_id) == "open"


# ---------------------------------------------------------------- notes and idempotency


async def test_note_replay_returns_original(provider: MockTicketing) -> None:
    ticket_id = await provider.new_ticket_id("tkey-note")

    first = await provider.add_note(ticket_id, "nkey-replay")
    second = await provider.add_note(ticket_id, "nkey-replay")

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert await provider.count("notes", "nkey-replay") == 1


async def test_note_key_with_different_body_returns_409(provider: MockTicketing) -> None:
    ticket_id = await provider.new_ticket_id("tkey-note-conflict")
    await provider.add_note(ticket_id, "nkey-conflict")

    response = await provider.add_note(ticket_id, "nkey-conflict", {"body": "changed"})

    assert response.status_code == 409
    assert response.json()["error_code"] == "IDEMPOTENCY_KEY_REUSED"
    assert await provider.count("notes", "nkey-conflict") == 1


async def test_note_key_reused_on_another_ticket_returns_409(provider: MockTicketing) -> None:
    first_ticket = await provider.new_ticket_id("tkey-a")
    second_ticket = await provider.new_ticket_id("tkey-b")
    await provider.add_note(first_ticket, "nkey-shared")

    response = await provider.add_note(second_ticket, "nkey-shared")

    assert response.status_code == 409
    assert await provider.count("notes", "nkey-shared") == 1


async def test_note_without_key_returns_400(provider: MockTicketing) -> None:
    ticket_id = await provider.new_ticket_id("tkey-note-nokey")

    response = await provider.add_note(ticket_id, None)

    assert response.status_code == 400
    assert response.json()["error_code"] == "IDEMPOTENCY_KEY_MISSING"
    assert (await provider.client.get(f"/tickets/{ticket_id}")).json()["notes"] == []


async def test_concurrent_duplicate_notes_create_one(provider: MockTicketing) -> None:
    ticket_id = await provider.new_ticket_id("tkey-concurrent")

    first, second = await asyncio.gather(
        provider.add_note(ticket_id, "nkey-concurrent"),
        provider.add_note(ticket_id, "nkey-concurrent"),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json()["id"] == second.json()["id"]
    assert await provider.count("notes", "nkey-concurrent") == 1


async def test_drop_response_on_note_still_saves_it_once(provider: MockTicketing) -> None:
    ticket_id = await provider.new_ticket_id("tkey-note-dropped")
    await provider.set_fault(mode="drop_response", count=1)

    dropped = await provider.add_note(ticket_id, "nkey-dropped")

    assert dropped.status_code == 504
    assert dropped.json()["retryable"] is True
    assert await provider.count("notes", "nkey-dropped") == 1
    retry = await provider.add_note(ticket_id, "nkey-dropped")
    assert retry.status_code == 200
    assert await provider.count("notes", "nkey-dropped") == 1


# ---------------------------------------------------------------- other fault modes


async def test_error_fault_on_note_saves_nothing(provider: MockTicketing) -> None:
    ticket_id = await provider.new_ticket_id("tkey-note-error")
    await provider.set_fault(mode="error", count=1)

    response = await provider.add_note(ticket_id, "nkey-error")

    assert response.status_code == 500
    assert response.json()["error_code"] == "PROVIDER_ERROR"
    assert await provider.count("notes", "nkey-error") == 0


async def test_slow_fault_delays_then_succeeds(provider: MockTicketing) -> None:
    ticket_id = await provider.new_ticket_id("tkey-slow")
    await provider.set_fault(mode="slow", count=1, delay_ms=300)

    started = time.monotonic()
    response = await provider.client.get(f"/tickets/{ticket_id}")

    assert response.status_code == 200
    # Not 0.3: on Windows the timer ticks about every 15.6 ms and asyncio may end a
    # sleep up to one tick early, so a 300 ms sleep can measure as ~285 ms.
    assert time.monotonic() - started >= 0.25


async def test_decline_fault_is_not_supported(provider: MockTicketing) -> None:
    response = await provider.client.post("/_admin/faults", json={"mode": "decline"})

    assert response.status_code == 422
    assert response.json()["error_code"] == "FAULT_MODE_UNSUPPORTED"


async def test_admin_routes_are_hidden_in_production(test_database_url: str) -> None:
    app = create_app(make_settings(test_database_url, app_env="production"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        get_response = await client.get("/_admin/faults")
        post_response = await client.post("/_admin/faults", json={"mode": "error"})

    assert get_response.status_code == 404
    assert post_response.status_code == 404
