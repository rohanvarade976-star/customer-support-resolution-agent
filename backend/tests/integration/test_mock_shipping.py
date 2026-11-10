"""Integration tests for the mock shipping provider (in-memory app, no server process).

A fixed fake clock makes every "days ago" value resolve to a known date.
The shipping mock needs no database.
"""

import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from mock_providers.shipping.app import create_app
from mock_providers.shipping.config import ShippingSettings

NOW = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
SCENARIOS_FILE = Path(__file__).resolve().parents[2] / "data" / "seed" / "scenarios.json"


def days_ago(days: int) -> date:
    return (NOW - timedelta(days=days)).date()


def make_app(app_env: str = "test") -> Any:
    settings = ShippingSettings(app_env=app_env)  # type: ignore[arg-type]  # Literal checked at runtime
    return create_app(settings, clock=lambda: NOW)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        yield client


async def set_fault(client: httpx.AsyncClient, **config: Any) -> None:
    response = await client.post("/_admin/faults", json=config)
    assert response.status_code == 200


def scenario_orders() -> list[dict[str, Any]]:
    data = json.loads(SCENARIOS_FILE.read_text(encoding="utf-8"))
    return [order for customer in data["customers"] for order in customer["orders"]]


# ---------------------------------------------------------------- lookups


async def test_arjun_laptop_is_held_at_hub_past_expected_delivery(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/shipments/ORD-1002")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "in_transit"
    assert body["carrier"]
    assert body["tracking_number"]
    assert body["delivered_at"] is None
    assert datetime.fromisoformat(body["shipped_at"]).date() == days_ago(8)
    assert date.fromisoformat(body["expected_delivery"]) == days_ago(3)  # already passed

    latest = body["events"][0]
    assert latest["description"] == "Held at hub"
    assert datetime.fromisoformat(latest["time"]).date() == days_ago(6)
    assert set(latest) == {"time", "location", "description"}


async def test_events_are_newest_first(client: httpx.AsyncClient) -> None:
    events = (await client.get("/shipments/ORD-1001")).json()["events"]

    times = [datetime.fromisoformat(event["time"]) for event in events]
    assert times == sorted(times, reverse=True)
    assert events[0]["description"] == "Delivered"


@pytest.mark.parametrize("order", scenario_orders(), ids=lambda order: order["order_number"])
async def test_every_scenario_order_matches_seed_data(
    client: httpx.AsyncClient, order: dict[str, Any]
) -> None:
    response = await client.get(f"/shipments/{order['order_number']}")

    assert response.status_code == 200
    body = response.json()
    if order["status"] == "delivered":
        assert body["status"] == "delivered"
        delivered_at = datetime.fromisoformat(body["delivered_at"])
        assert delivered_at.date() == days_ago(order["delivered_days_ago"])
        # Shipped after the order was placed.
        assert datetime.fromisoformat(body["shipped_at"]).date() >= days_ago(
            order["placed_days_ago"]
        )
    else:
        assert body["status"] == "in_transit"
        shipped_at = datetime.fromisoformat(body["shipped_at"])
        assert shipped_at.date() == days_ago(order["shipped_days_ago"])


async def test_unknown_order_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/shipments/ORD-9999")

    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "error_code": "SHIPMENT_NOT_FOUND",
        "retryable": False,
        "message": "No shipment for this order.",
    }


# ---------------------------------------------------------------- fault modes


async def test_error_fault_returns_500_once(client: httpx.AsyncClient) -> None:
    await set_fault(client, mode="error", count=1)

    failed = await client.get("/shipments/ORD-1002")
    recovered = await client.get("/shipments/ORD-1002")

    assert failed.status_code == 500
    assert failed.json()["error_code"] == "PROVIDER_ERROR"
    assert failed.json()["retryable"] is True
    assert recovered.status_code == 200


async def test_slow_fault_delays_then_succeeds(client: httpx.AsyncClient) -> None:
    await set_fault(client, mode="slow", count=1, delay_ms=300)

    started = time.monotonic()
    response = await client.get("/shipments/ORD-1002")

    assert response.status_code == 200
    # Not 0.3: on Windows the timer ticks about every 15.6 ms and asyncio may end a
    # sleep up to one tick early, so a 300 ms sleep can measure as ~285 ms.
    assert time.monotonic() - started >= 0.25


async def test_drop_response_returns_504_once(client: httpx.AsyncClient) -> None:
    await set_fault(client, mode="drop_response", count=1)

    dropped = await client.get("/shipments/ORD-1002")
    retry = await client.get("/shipments/ORD-1002")

    assert dropped.status_code == 504
    assert dropped.json()["error_code"] == "GATEWAY_TIMEOUT"
    assert retry.status_code == 200


async def test_decline_fault_is_not_supported(client: httpx.AsyncClient) -> None:
    response = await client.post("/_admin/faults", json={"mode": "decline"})

    assert response.status_code == 422
    assert response.json()["error_code"] == "FAULT_MODE_UNSUPPORTED"


async def test_admin_routes_are_hidden_in_production() -> None:
    transport = httpx.ASGITransport(app=make_app(app_env="production"))
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        get_response = await client.get("/_admin/faults")
        post_response = await client.post("/_admin/faults", json={"mode": "error"})

    assert get_response.status_code == 404
    assert post_response.status_code == 404
