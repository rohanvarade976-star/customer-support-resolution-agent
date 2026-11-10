"""Our provider clients against the real mock provider apps (in memory, no server process).

Each client talks to a mock through httpx.ASGITransport, wrapped in a CountingTransport
so tests can assert exactly how many requests reached the provider. Faults are switched
on through a separate admin client that is not counted.

Money and idempotency checks count rows in the mocks' own tables, not response text.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from mock_providers.payments.app import create_app as create_payments_app
from mock_providers.payments.config import PaymentsSettings
from mock_providers.shipping.app import create_app as create_shipping_app
from mock_providers.shipping.config import ShippingSettings
from mock_providers.ticketing.app import create_app as create_ticketing_app
from mock_providers.ticketing.config import TicketingSettings
from support_agent.core.config import Settings
from support_agent.domain.enums import RefundOperationStatus
from support_agent.integrations.errors import (
    OutcomeUnknownError,
    ProviderRejectedError,
    ProviderUnavailableError,
    ResourceNotFoundError,
)
from support_agent.integrations.payments import PaymentClient
from support_agent.integrations.shipping import ShippingClient
from support_agent.integrations.ticketing import ProviderTicketStatus, TicketingClient

REFUND: dict[str, Any] = {
    "order_reference": "ORD-1042",
    "amount_minor": 249900,
    "currency": "INR",
    "reason": "Return received and inspection passed",
}
TICKET: dict[str, Any] = {
    "customer_reference": "cust_arjun",
    "order_reference": "ORD-1002",
    "subject": "Laptop stuck in transit",
    "description": "My laptop has shown 'in transit' for 8 days.",
}
SLOW_MS = 1000  # far longer than the tiny timeout below


# ---------------------------------------------------------------- helpers


class CountingTransport(httpx.AsyncBaseTransport):
    """Passes requests to the mock app and remembers every one."""

    def __init__(self, app: FastAPI) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._inner.handle_async_request(request)

    def sent(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method]


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def client_settings(*, tiny_timeout: bool = False) -> Settings:
    timeout = 0.05 if tiny_timeout else 2.0
    return Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+asyncpg://user:pw@localhost:5432/unused"),
        jwt_secret=SecretStr("x" * 32),
        payments_base_url="http://payments.mock",
        shipping_base_url="http://shipping.mock",
        ticketing_base_url="http://ticketing.mock",
        provider_connect_timeout_seconds=timeout,
        provider_read_timeout_seconds=timeout,
        provider_max_attempts=3,
        provider_backoff_base_seconds=0.001,  # keep retries fast
    )


async def set_fault(app: FastAPI, **config: Any) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://admin") as admin:
        response = await admin.post("/_admin/faults", json=config)
    assert response.status_code == 200


async def count_rows(engine: AsyncEngine, table: str, key: str) -> int:
    # table is a fixed name from this file, never user input.
    sql = f"SELECT count(*) FROM {table} WHERE idempotency_key = :k"  # noqa: S608
    async with engine.connect() as conn:
        return int(await conn.scalar(text(sql), {"k": key}) or 0)


# ---------------------------------------------------------------- fixtures


@dataclass
class PaymentsEnv:
    app: FastAPI
    client: PaymentClient
    transport: CountingTransport
    clock: FakeClock
    engine: AsyncEngine

    async def refund_rows(self, key: str) -> int:
        return await count_rows(self.engine, "mock_payments.refunds", key)


@dataclass
class TicketingEnv:
    app: FastAPI
    client: TicketingClient
    transport: CountingTransport
    engine: AsyncEngine


@dataclass
class ShippingEnv:
    app: FastAPI
    client: ShippingClient
    transport: CountingTransport


@pytest.fixture
async def payments(test_database_url: str) -> AsyncIterator[PaymentsEnv]:
    clock = FakeClock()
    settings = PaymentsSettings(
        app_env="test",
        database_url=SecretStr(test_database_url),
        mock_payments_submitted_seconds=2,
        mock_payments_processing_seconds=5,
    )
    app = create_payments_app(settings, clock=clock)
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    # ASGITransport does not run startup/shutdown, so run the lifespan ourselves.
    async with app.router.lifespan_context(app):
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE mock_payments.refunds"))
        transport = CountingTransport(app)
        async with PaymentClient(client_settings(), transport=transport) as client:
            yield PaymentsEnv(app, client, transport, clock, engine)
    await engine.dispose()


@pytest.fixture
async def ticketing(test_database_url: str) -> AsyncIterator[TicketingEnv]:
    settings = TicketingSettings(app_env="test", database_url=SecretStr(test_database_url))
    app = create_ticketing_app(settings)
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    async with app.router.lifespan_context(app):
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE mock_ticketing.notes, mock_ticketing.tickets"))
        transport = CountingTransport(app)
        async with TicketingClient(client_settings(), transport=transport) as client:
            yield TicketingEnv(app, client, transport, engine)
    await engine.dispose()


@pytest.fixture
async def shipping() -> AsyncIterator[ShippingEnv]:
    # No database needed: the shipping mock reads a JSON file.
    app = create_shipping_app(ShippingSettings(app_env="test"))
    transport = CountingTransport(app)
    async with ShippingClient(client_settings(), transport=transport) as client:
        yield ShippingEnv(app, client, transport)


# ---------------------------------------------------------------- payments: create_refund


async def test_create_refund_returns_submitted_refund(payments: PaymentsEnv) -> None:
    refund = await payments.client.create_refund(idempotency_key="pay-create", **REFUND)

    assert refund.status is RefundOperationStatus.SUBMITTED
    assert refund.amount_minor == 249900
    assert refund.provider_reference.startswith("re_")
    assert await payments.refund_rows("pay-create") == 1


async def test_drop_response_raises_outcome_unknown_then_found_by_key(
    payments: PaymentsEnv,
) -> None:
    await set_fault(payments.app, mode="drop_response", count=1)

    with pytest.raises(OutcomeUnknownError) as caught:
        await payments.client.create_refund(idempotency_key="pay-drop", **REFUND)

    assert caught.value.idempotency_key == "pay-drop"
    assert len(payments.transport.sent("POST")) == 1  # never retried automatically
    # Reconciliation: the refund did happen, and asking by key reveals it.
    found = await payments.client.find_refund_by_key("pay-drop")
    assert found.amount_minor == 249900
    assert found.status is RefundOperationStatus.SUBMITTED
    assert await payments.refund_rows("pay-drop") == 1


async def test_500_raises_outcome_unknown_and_reconciliation_finds_nothing(
    payments: PaymentsEnv,
) -> None:
    await set_fault(payments.app, mode="error", count=3)

    with pytest.raises(OutcomeUnknownError):
        await payments.client.create_refund(idempotency_key="pay-500", **REFUND)

    assert len(payments.transport.sent("POST")) == 1
    # The mock's error fault saves nothing, so reconciliation says "no refund".
    with pytest.raises(ResourceNotFoundError):
        await payments.client.find_refund_by_key("pay-500")
    assert await payments.refund_rows("pay-500") == 0


async def test_create_refund_timeout_raises_outcome_unknown(payments: PaymentsEnv) -> None:
    await set_fault(payments.app, mode="slow", count=1, delay_ms=SLOW_MS)
    transport = CountingTransport(payments.app)

    async with PaymentClient(client_settings(tiny_timeout=True), transport=transport) as client:
        with pytest.raises(OutcomeUnknownError) as caught:
            await client.create_refund(idempotency_key="pay-slow", **REFUND)

    assert isinstance(caught.value.__cause__, ProviderUnavailableError)
    assert caught.value.__cause__.error_code == "TIMEOUT"
    assert len(transport.requests) == 1


async def test_reused_key_with_different_amount_is_rejected_once(payments: PaymentsEnv) -> None:
    await payments.client.create_refund(idempotency_key="pay-reuse", **REFUND)

    with pytest.raises(ProviderRejectedError) as caught:
        await payments.client.create_refund(
            idempotency_key="pay-reuse", **{**REFUND, "amount_minor": 100}
        )

    assert caught.value.status_code == 409
    assert caught.value.error_code == "IDEMPOTENCY_KEY_REUSED"
    assert len(payments.transport.sent("POST")) == 2  # one per call, no retries
    assert await payments.refund_rows("pay-reuse") == 1


# ---------------------------------------------------------------- payments: reads and status


async def test_status_mapping_follows_provider_lifecycle(payments: PaymentsEnv) -> None:
    created = await payments.client.create_refund(idempotency_key="pay-life", **REFUND)

    payments.clock.advance(3)
    processing = await payments.client.get_refund(created.provider_reference)
    payments.clock.advance(5)
    succeeded = await payments.client.get_refund(created.provider_reference)

    assert processing.status is RefundOperationStatus.PROCESSING
    assert succeeded.status is RefundOperationStatus.SUCCEEDED


async def test_declined_refund_maps_to_failed(payments: PaymentsEnv) -> None:
    await set_fault(payments.app, mode="decline", count=1)
    created = await payments.client.create_refund(idempotency_key="pay-decline", **REFUND)

    payments.clock.advance(10)
    refund = await payments.client.get_refund(created.provider_reference)

    assert refund.provider_status == "failed"
    assert refund.status is RefundOperationStatus.FAILED


async def test_get_refund_retries_after_error_fault(payments: PaymentsEnv) -> None:
    created = await payments.client.create_refund(idempotency_key="pay-read", **REFUND)
    await set_fault(payments.app, mode="error", count=1)

    refund = await payments.client.get_refund(created.provider_reference)

    assert refund.provider_reference == created.provider_reference
    assert len(payments.transport.sent("GET")) == 2  # one failed, one succeeded


async def test_unknown_refund_is_not_found_and_not_retried(payments: PaymentsEnv) -> None:
    with pytest.raises(ResourceNotFoundError) as caught:
        await payments.client.get_refund("re_does_not_exist")

    assert caught.value.error_code == "REFUND_NOT_FOUND"
    assert len(payments.transport.sent("GET")) == 1


# ---------------------------------------------------------------- shipping


async def test_get_shipment_returns_tracking(shipping: ShippingEnv) -> None:
    shipment = await shipping.client.get_shipment("ORD-1002")

    assert shipment.order_reference == "ORD-1002"
    assert shipment.status == "in_transit"
    assert shipment.events


async def test_shipment_retry_succeeds_after_error_fault(shipping: ShippingEnv) -> None:
    await set_fault(shipping.app, mode="error", count=2)

    shipment = await shipping.client.get_shipment("ORD-1002")

    assert shipment.order_reference == "ORD-1002"
    assert len(shipping.transport.requests) == 3


async def test_shipment_gives_up_after_max_attempts(shipping: ShippingEnv) -> None:
    await set_fault(shipping.app, mode="error", count=5)

    with pytest.raises(ProviderUnavailableError) as caught:
        await shipping.client.get_shipment("ORD-1002")

    assert caught.value.error_code == "PROVIDER_ERROR"
    assert len(shipping.transport.requests) == 3


async def test_unknown_shipment_is_not_found_and_not_retried(shipping: ShippingEnv) -> None:
    with pytest.raises(ResourceNotFoundError) as caught:
        await shipping.client.get_shipment("ORD-9999")

    assert caught.value.error_code == "SHIPMENT_NOT_FOUND"
    assert len(shipping.transport.requests) == 1


async def test_shipment_timeout_retried_then_raises(shipping: ShippingEnv) -> None:
    await set_fault(shipping.app, mode="slow", count=3, delay_ms=SLOW_MS)
    transport = CountingTransport(shipping.app)

    async with ShippingClient(client_settings(tiny_timeout=True), transport=transport) as client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await client.get_shipment("ORD-1002")

    assert caught.value.error_code == "TIMEOUT"
    assert len(transport.requests) == 3


async def test_shipment_timeout_then_success_on_retry(shipping: ShippingEnv) -> None:
    await set_fault(shipping.app, mode="slow", count=1, delay_ms=SLOW_MS)
    transport = CountingTransport(shipping.app)

    async with ShippingClient(client_settings(tiny_timeout=True), transport=transport) as client:
        shipment = await client.get_shipment("ORD-1002")

    assert shipment.order_reference == "ORD-1002"
    assert len(transport.requests) == 2


# ---------------------------------------------------------------- ticketing


async def test_create_and_get_ticket(ticketing: TicketingEnv) -> None:
    created = await ticketing.client.create_ticket(idempotency_key="tkt-create", **TICKET)

    fetched = await ticketing.client.get_ticket(created.id)

    assert fetched.status is ProviderTicketStatus.OPEN
    assert fetched.subject == TICKET["subject"]
    assert await count_rows(ticketing.engine, "mock_ticketing.tickets", "tkt-create") == 1


async def test_create_ticket_retried_after_error_creates_one(ticketing: TicketingEnv) -> None:
    await set_fault(ticketing.app, mode="error", count=1)

    await ticketing.client.create_ticket(idempotency_key="tkt-retry", **TICKET)

    assert len(ticketing.transport.sent("POST")) == 2
    assert await count_rows(ticketing.engine, "mock_ticketing.tickets", "tkt-retry") == 1


async def test_note_with_dropped_response_is_retried_and_stored_once(
    ticketing: TicketingEnv,
) -> None:
    ticket = await ticketing.client.create_ticket(idempotency_key="tkt-note", **TICKET)
    await set_fault(ticketing.app, mode="drop_response", count=1)

    note = await ticketing.client.add_note(ticket.id, "Refund approved.", idempotency_key="n-1")

    note_posts = [r for r in ticketing.transport.sent("POST") if r.url.path.endswith("/notes")]
    assert len(note_posts) == 2  # the first reply was lost, so it was retried
    assert {r.headers["Idempotency-Key"] for r in note_posts} == {"n-1"}  # same key both times
    assert note.body == "Refund approved."
    assert await count_rows(ticketing.engine, "mock_ticketing.notes", "n-1") == 1


async def test_update_status_retried_after_dropped_response(ticketing: TicketingEnv) -> None:
    ticket = await ticketing.client.create_ticket(idempotency_key="tkt-status", **TICKET)
    await set_fault(ticketing.app, mode="drop_response", count=1)

    updated = await ticketing.client.update_status(
        ticket.id, ProviderTicketStatus.RESOLVED, idempotency_key="s-1"
    )

    patches = ticketing.transport.sent("PATCH")
    assert len(patches) == 2
    assert all(r.headers["Idempotency-Key"] == "s-1" for r in patches)
    assert updated.status is ProviderTicketStatus.RESOLVED


async def test_note_on_unknown_ticket_is_not_found_and_not_retried(
    ticketing: TicketingEnv,
) -> None:
    with pytest.raises(ResourceNotFoundError) as caught:
        await ticketing.client.add_note("tkt_missing", "Hello", idempotency_key="n-missing")

    assert caught.value.error_code == "TICKET_NOT_FOUND"
    assert len(ticketing.transport.requests) == 1


async def test_reused_ticket_key_with_changed_body_is_rejected_once(
    ticketing: TicketingEnv,
) -> None:
    await ticketing.client.create_ticket(idempotency_key="tkt-reuse", **TICKET)

    with pytest.raises(ProviderRejectedError) as caught:
        await ticketing.client.create_ticket(
            idempotency_key="tkt-reuse", **{**TICKET, "subject": "Different"}
        )

    assert caught.value.error_code == "IDEMPOTENCY_KEY_REUSED"
    assert len(ticketing.transport.sent("POST")) == 2
    assert await count_rows(ticketing.engine, "mock_ticketing.tickets", "tkt-reuse") == 1
