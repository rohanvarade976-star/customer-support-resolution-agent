"""Integration tests for the mock payment provider (real Postgres, no server process).

httpx.ASGITransport sends requests straight into the FastAPI app in memory.
A fake clock lets us move time forward instead of sleeping.
Money/idempotency checks assert database rows, not just response text.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from mock_providers.payments.app import create_app
from mock_providers.payments.config import PaymentsSettings

SUBMITTED_SECONDS = 2
PROCESSING_SECONDS = 5

PAYLOAD: dict[str, Any] = {
    "order_reference": "ord_1042",
    "amount_minor": 249900,
    "currency": "INR",
    "reason": "Return received and inspection passed",
}


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class MockProvider:
    client: httpx.AsyncClient
    clock: FakeClock
    engine: AsyncEngine

    async def create(self, key: str, payload: dict[str, Any] | None = None) -> httpx.Response:
        return await self.client.post(
            "/refunds", json=payload or PAYLOAD, headers={"Idempotency-Key": key}
        )

    async def set_fault(self, **config: Any) -> None:
        response = await self.client.post("/_admin/faults", json=config)
        assert response.status_code == 200

    async def row_count(self, key: str) -> int:
        async with self.engine.connect() as conn:
            count = await conn.scalar(
                text("SELECT count(*) FROM mock_payments.refunds WHERE idempotency_key = :k"),
                {"k": key},
            )
        return int(count or 0)


def make_settings(database_url: str, app_env: str = "test") -> PaymentsSettings:
    return PaymentsSettings(
        app_env=app_env,  # type: ignore[arg-type]  # Literal checked by pydantic at runtime
        database_url=SecretStr(database_url),
        mock_payments_submitted_seconds=SUBMITTED_SECONDS,
        mock_payments_processing_seconds=PROCESSING_SECONDS,
    )


@pytest.fixture
async def provider(test_database_url: str) -> AsyncIterator[MockProvider]:
    clock = FakeClock()
    app = create_app(make_settings(test_database_url), clock=clock)
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    # ASGITransport does not run startup/shutdown, so run the lifespan ourselves.
    async with app.router.lifespan_context(app):
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE mock_payments.refunds"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
            yield MockProvider(client=client, clock=clock, engine=engine)
    await engine.dispose()


# ---------------------------------------------------------------- create and idempotency


async def test_create_returns_201_with_submitted_refund(provider: MockProvider) -> None:
    response = await provider.create("key-create")

    assert response.status_code == 201
    body = response.json()
    assert body["provider_reference"].startswith("re_")
    assert body["status"] == "submitted"
    assert body["amount_minor"] == 249900
    assert await provider.row_count("key-create") == 1


async def test_replay_with_same_payload_returns_original(provider: MockProvider) -> None:
    first = await provider.create("key-replay")
    second = await provider.create("key-replay")

    assert second.status_code == 200
    assert second.json()["provider_reference"] == first.json()["provider_reference"]
    assert await provider.row_count("key-replay") == 1


async def test_same_key_with_changed_payload_returns_409(provider: MockProvider) -> None:
    await provider.create("key-conflict")
    changed = {**PAYLOAD, "amount_minor": 999900}

    response = await provider.create("key-conflict", changed)

    assert response.status_code == 409
    assert response.json() == {
        "success": False,
        "error_code": "IDEMPOTENCY_KEY_REUSED",
        "retryable": False,
        "message": response.json()["message"],
    }
    # The original refund is untouched and no second one exists.
    assert await provider.row_count("key-conflict") == 1
    lookup = await provider.client.get("/refunds", params={"idempotency_key": "key-conflict"})
    assert lookup.json()["amount_minor"] == 249900


async def test_concurrent_identical_requests_create_one_refund(provider: MockProvider) -> None:
    first, second = await asyncio.gather(
        provider.create("key-concurrent"), provider.create("key-concurrent")
    )

    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json()["provider_reference"] == second.json()["provider_reference"]
    assert await provider.row_count("key-concurrent") == 1


async def test_missing_idempotency_key_returns_400(provider: MockProvider) -> None:
    response = await provider.client.post("/refunds", json=PAYLOAD)

    assert response.status_code == 400
    assert response.json()["error_code"] == "IDEMPOTENCY_KEY_MISSING"


async def test_float_amount_is_rejected(provider: MockProvider) -> None:
    response = await provider.create("key-float", {**PAYLOAD, "amount_minor": 2499.0})

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert await provider.row_count("key-float") == 0


# ---------------------------------------------------------------- lookups and lifecycle


async def test_lookup_by_reference_and_by_key(provider: MockProvider) -> None:
    reference = (await provider.create("key-lookup")).json()["provider_reference"]

    by_reference = await provider.client.get(f"/refunds/{reference}")
    by_key = await provider.client.get("/refunds", params={"idempotency_key": "key-lookup"})

    assert by_reference.status_code == 200
    assert by_key.status_code == 200
    assert by_key.json()["provider_reference"] == reference


async def test_unknown_refund_returns_404(provider: MockProvider) -> None:
    by_key = await provider.client.get("/refunds", params={"idempotency_key": "never-used"})
    by_reference = await provider.client.get("/refunds/re_missing")

    for response in (by_key, by_reference):
        assert response.status_code == 404
        assert response.json()["error_code"] == "REFUND_NOT_FOUND"


async def test_lifecycle_progresses_with_time(provider: MockProvider) -> None:
    reference = (await provider.create("key-lifecycle")).json()["provider_reference"]

    async def status() -> str:
        return str((await provider.client.get(f"/refunds/{reference}")).json()["status"])

    assert await status() == "submitted"
    provider.clock.advance(SUBMITTED_SECONDS)
    assert await status() == "processing"
    provider.clock.advance(PROCESSING_SECONDS)
    assert await status() == "succeeded"


# ---------------------------------------------------------------- fault modes


async def test_error_fault_returns_500_and_saves_nothing(provider: MockProvider) -> None:
    await provider.set_fault(mode="error", count=1)

    response = await provider.create("key-error")

    assert response.status_code == 500
    assert response.json() == {
        "success": False,
        "error_code": "PROVIDER_ERROR",
        "retryable": True,
        "message": "Internal provider error.",
    }
    assert await provider.row_count("key-error") == 0


async def test_slow_fault_delays_then_succeeds(provider: MockProvider) -> None:
    await provider.set_fault(mode="slow", count=1, delay_ms=300)

    started = time.monotonic()
    response = await provider.create("key-slow")
    elapsed = time.monotonic() - started

    assert response.status_code == 201
    assert elapsed >= 0.3
    assert await provider.row_count("key-slow") == 1


async def test_drop_response_saves_refund_but_returns_504(provider: MockProvider) -> None:
    await provider.set_fault(mode="drop_response", count=1)

    response = await provider.create("key-dropped")

    assert response.status_code == 504
    assert response.json()["error_code"] == "GATEWAY_TIMEOUT"
    assert response.json()["retryable"] is True
    # The refund was created even though the caller never saw it...
    assert await provider.row_count("key-dropped") == 1
    # ...so reconciliation by key finds it,
    found = await provider.client.get("/refunds", params={"idempotency_key": "key-dropped"})
    assert found.status_code == 200
    # and a safe retry with the same key returns that same refund instead of a new one.
    retry = await provider.create("key-dropped")
    assert retry.status_code == 200
    assert retry.json()["provider_reference"] == found.json()["provider_reference"]
    assert await provider.row_count("key-dropped") == 1


async def test_decline_fault_makes_refund_end_as_failed(provider: MockProvider) -> None:
    await provider.set_fault(mode="decline", count=1)

    declined = (await provider.create("key-declined")).json()
    normal = (await provider.create("key-normal")).json()
    assert declined["status"] == "submitted"  # failure only shows up after processing

    provider.clock.advance(SUBMITTED_SECONDS + PROCESSING_SECONDS)
    declined_now = await provider.client.get(f"/refunds/{declined['provider_reference']}")
    normal_now = await provider.client.get(f"/refunds/{normal['provider_reference']}")

    assert declined_now.json()["status"] == "failed"
    assert normal_now.json()["status"] == "succeeded"


async def test_fault_stops_after_count_requests(provider: MockProvider) -> None:
    await provider.set_fault(mode="error", count=2)

    codes = [(await provider.create(f"key-count-{i}")).status_code for i in range(3)]

    assert codes == [500, 500, 201]
    state = (await provider.client.get("/_admin/faults")).json()
    assert state == {"mode": "none", "remaining": 0, "delay_ms": 0}


async def test_admin_routes_are_hidden_in_production(test_database_url: str) -> None:
    app = create_app(make_settings(test_database_url, app_env="production"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        get_response = await client.get("/_admin/faults")
        post_response = await client.post("/_admin/faults", json={"mode": "error"})

    assert get_response.status_code == 404
    assert post_response.status_code == 404
    assert get_response.json()["success"] is False
