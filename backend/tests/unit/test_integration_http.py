"""Unit tests for the provider HTTP layer: no database, no mock apps.

A scripted fake transport plays the network: each request gets the next scripted
outcome (a response or a network exception), and every request is counted.
"""

import logging
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from support_agent.core.config import Settings
from support_agent.domain.enums import RefundOperationStatus
from support_agent.integrations.errors import (
    OutcomeUnknownError,
    ProviderRejectedError,
    ProviderUnavailableError,
    ResourceNotFoundError,
)
from support_agent.integrations.http import backoff_delay, error_from_response
from support_agent.integrations.payments import PaymentClient, ProviderRefund, map_refund_status
from support_agent.integrations.shipping import ShippingClient

KEY = "refund-key-123"
REFUND_ARGS: dict[str, Any] = {
    "idempotency_key": KEY,
    "order_reference": "ORD-1042",
    "amount_minor": 249900,
    "currency": "INR",
    "reason": "Return received and inspection passed",
}
REFUND_BODY: dict[str, Any] = {
    "provider_reference": "re_1",
    "idempotency_key": KEY,
    "order_reference": "ORD-1042",
    "amount_minor": 249900,
    "currency": "INR",
    "reason": "Return received and inspection passed",
    "status": "submitted",
    "created_at": "2026-01-01T12:00:00Z",
}
SHIPMENT_BODY: dict[str, Any] = {
    "order_reference": "ORD-1002",
    "status": "in_transit",
    "carrier": "BlueDart",
    "tracking_number": "BD123",
    "shipped_at": "2026-01-01T10:00:00Z",
    "expected_delivery": "2026-01-05",
    "delivered_at": None,
    "events": [],
}
UNAVAILABLE = (503, {"error_code": "PROVIDER_ERROR", "retryable": True, "message": "down"})

Outcome = Exception | tuple[int, Any]


class ScriptedTransport(httpx.AsyncBaseTransport):
    """Answers request N with outcome N; the last outcome repeats forever."""

    def __init__(self, *outcomes: Outcome) -> None:
        self.outcomes = outcomes
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.outcomes[min(len(self.requests), len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+asyncpg://user:pw@localhost:5432/unused"),
        provider_connect_timeout_seconds=1.0,
        provider_read_timeout_seconds=1.0,
        provider_max_attempts=3,
        provider_backoff_base_seconds=0.001,  # keep retries fast
    )


# ---------------------------------------------------------------- status mapping


@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [
        ("submitted", RefundOperationStatus.SUBMITTED),
        ("processing", RefundOperationStatus.PROCESSING),
        ("succeeded", RefundOperationStatus.SUCCEEDED),
        ("failed", RefundOperationStatus.FAILED),
        ("reversed", RefundOperationStatus.PENDING_RECONCILIATION),  # unknown: never guess
        ("", RefundOperationStatus.PENDING_RECONCILIATION),
    ],
)
def test_map_refund_status(provider_status: str, expected: RefundOperationStatus) -> None:
    assert map_refund_status(provider_status) is expected


def test_provider_refund_exposes_mapped_status() -> None:
    refund = ProviderRefund.model_validate({**REFUND_BODY, "status": "processing"})

    assert refund.provider_status == "processing"
    assert refund.status is RefundOperationStatus.PROCESSING


# ---------------------------------------------------------------- backoff


@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 5])
def test_backoff_stays_between_zero_and_doubling_ceiling(attempt: int) -> None:
    ceiling = 0.2 * 2 ** (attempt - 1)
    delays = [backoff_delay(attempt, 0.2) for _ in range(200)]

    assert all(0 <= delay <= ceiling for delay in delays)
    assert len(set(delays)) > 1  # jitter: not the same wait every time


# ---------------------------------------------------------------- error mapping


def test_structured_5xx_maps_to_unavailable() -> None:
    error = error_from_response("p", httpx.Response(UNAVAILABLE[0], json=UNAVAILABLE[1]))

    assert isinstance(error, ProviderUnavailableError)
    assert error.error_code == "PROVIDER_ERROR"
    assert error.request_sent is True


def test_404_maps_to_not_found() -> None:
    body = {"error_code": "REFUND_NOT_FOUND", "retryable": False, "message": "nope"}

    error = error_from_response("p", httpx.Response(404, json=body))

    assert isinstance(error, ResourceNotFoundError)
    assert error.error_code == "REFUND_NOT_FOUND"


def test_4xx_maps_to_rejected_even_if_provider_says_retryable() -> None:
    body = {"error_code": "IDEMPOTENCY_KEY_REUSED", "retryable": True, "message": "reused"}

    error = error_from_response("p", httpx.Response(409, json=body))

    assert isinstance(error, ProviderRejectedError)
    assert error.status_code == 409
    assert error.error_code == "IDEMPOTENCY_KEY_REUSED"


def test_unstructured_error_body_gets_http_status_code() -> None:
    error = error_from_response("p", httpx.Response(502, content=b"<html>Bad gateway</html>"))

    assert isinstance(error, ProviderUnavailableError)
    assert error.error_code == "HTTP_502"


# ---------------------------------------------------------------- retried reads


async def test_read_retries_5xx_then_succeeds() -> None:
    transport = ScriptedTransport(UNAVAILABLE, UNAVAILABLE, (200, SHIPMENT_BODY))
    async with ShippingClient(make_settings(), transport=transport) as client:
        shipment = await client.get_shipment("ORD-1002")

    assert shipment.order_reference == "ORD-1002"
    assert len(transport.requests) == 3


async def test_read_gives_up_after_max_attempts() -> None:
    transport = ScriptedTransport(httpx.ConnectError("refused"))
    async with ShippingClient(make_settings(), transport=transport) as client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await client.get_shipment("ORD-1002")

    assert caught.value.error_code == "CONNECTION_FAILED"
    assert len(transport.requests) == 3


async def test_read_never_retries_4xx() -> None:
    body = {"error_code": "VALIDATION_ERROR", "retryable": False, "message": "bad"}
    transport = ScriptedTransport((400, body))
    async with ShippingClient(make_settings(), transport=transport) as client:
        with pytest.raises(ProviderRejectedError):
            await client.get_shipment("ORD-1002")

    assert len(transport.requests) == 1


# ---------------------------------------------------------------- refund creation (ADR-004)


async def test_create_refund_connect_failure_is_unavailable_not_unknown() -> None:
    transport = ScriptedTransport(httpx.ConnectError("refused"))
    async with PaymentClient(make_settings(), transport=transport) as client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await client.create_refund(**REFUND_ARGS)

    assert caught.value.request_sent is False  # nothing was sent, so no refund exists
    assert len(transport.requests) == 1  # and still not retried


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("connection closed mid-response"),
        (500, {"error_code": "PROVIDER_ERROR", "retryable": True, "message": "boom"}),
        (504, {"error_code": "GATEWAY_TIMEOUT", "retryable": True, "message": "lost"}),
        (201, b"not json"),
    ],
    ids=["read-timeout", "dropped-connection", "500", "504", "unreadable-2xx"],
)
async def test_create_refund_uncertain_failures_raise_outcome_unknown(outcome: Outcome) -> None:
    transport = ScriptedTransport(outcome)
    async with PaymentClient(make_settings(), transport=transport) as client:
        with pytest.raises(OutcomeUnknownError) as caught:
            await client.create_refund(**REFUND_ARGS)

    assert caught.value.idempotency_key == KEY
    assert len(transport.requests) == 1  # never retried automatically


async def test_create_refund_sends_idempotency_key_and_minor_units() -> None:
    transport = ScriptedTransport((201, REFUND_BODY))
    async with PaymentClient(make_settings(), transport=transport) as client:
        refund = await client.create_refund(**REFUND_ARGS)

    sent = transport.requests[0]
    assert sent.headers["Idempotency-Key"] == KEY
    assert b'"amount_minor":249900' in sent.content
    assert refund.status is RefundOperationStatus.SUBMITTED


# ---------------------------------------------------------------- logging


async def test_each_attempt_is_logged_without_keys(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="support_agent.integrations.http")
    transport = ScriptedTransport(UNAVAILABLE, (200, REFUND_BODY))
    async with PaymentClient(make_settings(), transport=transport) as client:
        await client.find_refund_by_key(KEY)

    records = [r for r in caplog.records if r.name == "support_agent.integrations.http"]
    assert [(r.__dict__["attempt"], r.__dict__["status"]) for r in records] == [(1, 503), (2, 200)]
    first = records[0].__dict__
    assert first["provider"] == "payments"
    assert first["method"] == "GET"
    assert first["path"] == "/refunds"
    assert first["outcome"] == "ProviderUnavailableError"
    assert first["latency_ms"] >= 0
    # The idempotency key travels in the query string and must never be logged.
    assert all(KEY not in str(r.__dict__) for r in records)
