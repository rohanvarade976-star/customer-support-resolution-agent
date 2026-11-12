"""Read tools against the seeded database and the in-memory mock shipping provider.

Each test runs as a seeded customer through the registry, exactly as an agent would.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mock_providers.shipping.app import create_app as create_shipping_app
from mock_providers.shipping.config import ShippingSettings
from support_agent.core.config import Settings
from support_agent.db.models import Customer, Order, PolicyDocument, Return
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.eligibility import EligibilityDecision, ReasonCode
from support_agent.domain.enums import (
    InspectionStatus,
    OrderStatus,
    RefundOperationStatus,
    ReturnStatus,
)
from support_agent.integrations.shipping import Shipment, ShippingClient
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.schemas.order_status import (
    PaymentStatusResponse,
    RefundableBalanceResponse,
    ReturnStatusResponse,
)
from support_agent.schemas.orders import OrderResponse
from support_agent.tools.base import ToolContext, ToolError
from support_agent.tools.read_tools import build_read_registry

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
R = ReasonCode
registry = build_read_registry()

type MakeCtx = Callable[..., Awaitable[ToolContext]]
type Run = Callable[[str, str, str], Awaitable[BaseModel | ToolError]]


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+asyncpg://u:p@localhost/unused"),
        jwt_secret=SecretStr("x" * 32),
        shipping_base_url="http://shipping.mock",
        provider_max_attempts=1,  # failures are asserted directly, so no retries
        provider_backoff_base_seconds=0,
        **overrides,
    )


class CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str, ingested_policies: None) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


@pytest.fixture
async def shipping_transport() -> CountingTransport:
    app = create_shipping_app(ShippingSettings(app_env="test"), clock=lambda: NOW)
    return CountingTransport(httpx.ASGITransport(app=app))


@pytest.fixture
async def make_ctx(
    session: AsyncSession, shipping_transport: CountingTransport
) -> AsyncIterator[MakeCtx]:
    """make_ctx("priya") builds the context for the seeded customer with that email name."""
    clients: list[ShippingClient] = []

    async def build(name: str, transport: httpx.AsyncBaseTransport | None = None) -> ToolContext:
        customer_id: UUID = await session.scalar(
            select(Customer.id).where(Customer.email == f"{name}@example.com")
        )  # type: ignore[assignment]
        settings = make_settings()
        client = ShippingClient(settings, transport=transport or shipping_transport)
        clients.append(client)
        return ToolContext(
            session, customer_id, settings, client, FakeEmbedder(), clock=lambda: NOW
        )

    yield build
    for client in clients:
        await client.aclose()


@pytest.fixture
def run(make_ctx: MakeCtx) -> Run:
    async def do_run(customer: str, tool: str, order_number: str) -> BaseModel | ToolError:
        ctx = await make_ctx(customer)
        return await registry.execute(tool, ctx, {"order_number": order_number})

    return do_run


def expect[T: BaseModel](result: BaseModel | ToolError, model: type[T]) -> T:
    assert isinstance(result, model), result
    return result


def expect_error(result: BaseModel | ToolError, code: str, *, retryable: bool = False) -> ToolError:
    assert isinstance(result, ToolError), result
    assert (result.success, result.error_code, result.retryable) == (False, code, retryable)
    return result


# ---------------------------------------------------------------- eligibility, every scenario

ELIGIBILITY = [
    ("priya", "ORD-1001", True, 249900, []),
    ("arjun", "ORD-1002", False, 0, [R.NOT_DELIVERED]),
    ("meera", "ORD-1003", False, 0, [R.NO_RETURN]),
    ("rahul", "ORD-1004", False, 0, [R.NO_RETURN, R.OUTSIDE_RETURN_WINDOW]),
    ("kavya", "ORD-1005", False, 0, [R.ALREADY_REFUNDED]),
    ("vikram", "ORD-1006", False, 0, [R.INSPECTION_FAILED]),
    ("aditya", "ORD-1007", False, 0, [R.RETURN_NOT_RECEIVED, R.INSPECTION_PENDING]),
    ("sneha", "ORD-1008", False, 0, [R.NO_RETURN]),
    ("sneha", "ORD-1009", False, 0, [R.NO_RETURN]),
]


@pytest.mark.parametrize(
    ("customer", "order", "eligible", "amount", "reasons"),
    ELIGIBILITY,
    ids=[f"{customer}-{order}" for customer, order, *_ in ELIGIBILITY],
)
async def test_check_refund_eligibility_for_every_seeded_order(
    run: Run, customer: str, order: str, eligible: bool, amount: int, reasons: list[ReasonCode]
) -> None:
    decision = expect(await run(customer, "check_refund_eligibility", order), EligibilityDecision)

    assert decision.eligible is eligible
    assert decision.refundable_amount_minor == amount
    assert decision.reasons == reasons


async def test_refund_window_comes_from_the_policy_row(run: Run, session: AsyncSession) -> None:
    # Rahul's order (placed 64 days ago, so under v2) was delivered 60 days ago. Widening
    # v2's window in the database to 90 days removes only the window reason: the number
    # comes from the policy in force, not from code or config (ADR-005).
    v2 = await session.scalar(
        select(PolicyDocument).where(
            PolicyDocument.policy_key == "refund_policy", PolicyDocument.version == "v2"
        )
    )
    assert v2 is not None
    v2.refund_window_days = 90
    await session.flush()

    decision = expect(
        await run("rahul", "check_refund_eligibility", "ORD-1004"), EligibilityDecision
    )

    assert decision.reasons == [R.NO_RETURN]


async def add_returned_order(
    session: AsyncSession, customer: str, order_number: str, placed_at: datetime, return_day: int
) -> None:
    """A delivered order whose return was requested `return_day` days after delivery, then
    received and passed inspection: eligible unless the window or policy says otherwise."""
    customer_id = await session.scalar(
        select(Customer.id).where(Customer.email == f"{customer}@example.com")
    )
    assert customer_id is not None
    delivered_at = placed_at + timedelta(days=5)
    order = Order(
        order_number=order_number,
        customer_id=customer_id,
        status=OrderStatus.DELIVERED,
        paid_amount_minor=150000,
        placed_at=placed_at,
        delivered_at=delivered_at,
    )
    session.add(order)
    await session.flush()
    requested_at = delivered_at + timedelta(days=return_day)
    session.add(
        Return(
            order_id=order.id,
            status=ReturnStatus.RECEIVED,
            received_at=requested_at + timedelta(days=3),
            inspection_status=InspectionStatus.PASSED,
            created_at=requested_at,
        )
    )
    await session.flush()


async def test_june_2026_order_gets_the_15_day_window_of_policy_v1(
    run: Run, session: AsyncSession
) -> None:
    # Placed 2026-06-20, so refund policy v1 (15 days) applies. A return on day 20 is
    # inside v2's 30 days but outside v1's 15; a return on day 14 is inside both.
    placed = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    await add_returned_order(session, "priya", "ORD-2001", placed, return_day=20)
    await add_returned_order(session, "priya", "ORD-2002", placed, return_day=14)

    late = expect(await run("priya", "check_refund_eligibility", "ORD-2001"), EligibilityDecision)
    in_time = expect(
        await run("priya", "check_refund_eligibility", "ORD-2002"), EligibilityDecision
    )

    assert (late.eligible, late.reasons) == (False, [R.OUTSIDE_RETURN_WINDOW])
    assert (in_time.eligible, in_time.refundable_amount_minor) == (True, 150000)


async def test_order_placed_before_any_refund_policy_has_no_applicable_policy(
    run: Run, session: AsyncSession
) -> None:
    # Refund policy v1 starts on 2025-01-01. Apart from that, this order is eligible.
    placed = datetime(2024, 12, 1, 10, 0, tzinfo=UTC)
    await add_returned_order(session, "priya", "ORD-2003", placed, return_day=2)

    decision = expect(
        await run("priya", "check_refund_eligibility", "ORD-2003"), EligibilityDecision
    )

    assert decision.eligible is False
    assert decision.refundable_amount_minor == 0
    assert decision.reasons == [R.NO_APPLICABLE_POLICY]


# ---------------------------------------------------------------- the other read tools


async def test_get_order(run: Run) -> None:
    order = expect(await run("priya", "get_order", "ORD-1001"), OrderResponse)

    assert order.order_number == "ORD-1001"
    assert order.status == OrderStatus.DELIVERED
    assert order.paid_amount_minor == 249900


async def test_get_return_status_for_a_received_and_an_open_return(run: Run) -> None:
    priya = expect(await run("priya", "get_return_status", "ORD-1001"), ReturnStatusResponse)
    aditya = expect(await run("aditya", "get_return_status", "ORD-1007"), ReturnStatusResponse)
    meera = expect(await run("meera", "get_return_status", "ORD-1003"), ReturnStatusResponse)

    assert (priya.return_status, priya.inspection_status) == (
        ReturnStatus.RECEIVED,
        InspectionStatus.PASSED,
    )
    assert priya.received_at is not None
    assert (aditya.return_status, aditya.received_at) == (ReturnStatus.REQUESTED, None)
    assert meera.return_status is None and meera.requested_at is None


async def test_get_payment_status_shows_prior_refunds(run: Run) -> None:
    kavya = expect(await run("kavya", "get_payment_status", "ORD-1005"), PaymentStatusResponse)
    priya = expect(await run("priya", "get_payment_status", "ORD-1001"), PaymentStatusResponse)

    assert kavya.paid_amount_minor == kavya.succeeded_refunds_minor == 399900
    assert [(r.status, r.amount_minor) for r in kavya.refunds] == [
        (RefundOperationStatus.SUCCEEDED, 399900)
    ]
    assert priya.refunds == [] and priya.in_progress_refunds_minor == 0
    assert "idempotency_key" not in kavya.model_dump_json()


async def test_get_refundable_balance(run: Run) -> None:
    priya = expect(
        await run("priya", "get_refundable_balance", "ORD-1001"), RefundableBalanceResponse
    )
    kavya = expect(
        await run("kavya", "get_refundable_balance", "ORD-1005"), RefundableBalanceResponse
    )

    assert priya.refundable_balance_minor == 249900
    assert kavya.refundable_balance_minor == 0


async def test_get_shipment_returns_carrier_tracking(run: Run) -> None:
    shipment = expect(await run("arjun", "get_shipment", "ORD-1002"), Shipment)

    assert shipment.order_reference == "ORD-1002"
    assert shipment.status == "in_transit"
    assert shipment.events  # newest first


# ---------------------------------------------------------------- ownership


READ_TOOLS = [
    "get_order",
    "get_return_status",
    "get_payment_status",
    "get_shipment",
    "get_refundable_balance",
    "check_refund_eligibility",
]


def test_registry_has_every_read_tool_and_search_policy() -> None:
    assert registry.names == [*READ_TOOLS, "search_policy"]


@pytest.mark.parametrize("tool", READ_TOOLS)
async def test_another_customers_order_looks_exactly_like_a_missing_one(
    run: Run, tool: str
) -> None:
    others = await run("rahul", tool, "ORD-1001")  # Priya's order
    missing = await run("rahul", tool, "ORD-9999")  # does not exist

    expect_error(others, "ORDER_NOT_FOUND")
    assert others == missing  # same code and message: no hint that ORD-1001 exists


async def test_provider_is_never_asked_about_another_customers_shipment(
    run: Run, shipping_transport: CountingTransport
) -> None:
    await run("rahul", "get_shipment", "ORD-1001")

    assert shipping_transport.requests == []


# ---------------------------------------------------------------- input validation


async def test_input_with_a_customer_id_is_rejected(make_ctx: MakeCtx) -> None:
    ctx = await make_ctx("rahul")
    priya_id = await ctx.session.scalar(
        select(Customer.id).where(Customer.email == "priya@example.com")
    )

    result = await registry.execute(
        "get_order", ctx, {"order_number": "ORD-1001", "customer_id": str(priya_id)}
    )

    expect_error(result, "INVALID_INPUT")


@pytest.mark.parametrize(
    "bad", ["", "1001", "ord-1001", "ORD-1001; DROP TABLE orders", "ORD-" + "9" * 30]
)
async def test_malformed_order_numbers_are_rejected(run: Run, bad: str) -> None:
    expect_error(await run("priya", "get_order", bad), "INVALID_INPUT")


async def test_internal_uuid_is_not_accepted_as_an_order_reference(
    run: Run, session: AsyncSession
) -> None:
    from support_agent.db.models import Order

    order_id = await session.scalar(select(Order.id).where(Order.order_number == "ORD-1001"))

    expect_error(await run("priya", "get_order", str(order_id)), "INVALID_INPUT")


# ---------------------------------------------------------------- provider failures


def fixed_response(status: int, body: dict[str, Any]) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _request: httpx.Response(status, json=body))


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (503, "SHIPPING_UNAVAILABLE", True),
        (404, "SHIPMENT_NOT_FOUND", False),
        (400, "SHIPPING_ERROR", False),
    ],
)
async def test_shipping_failures_become_structured_errors(
    make_ctx: MakeCtx, status: int, code: str, retryable: bool
) -> None:
    transport = fixed_response(status, {"error_code": "X", "message": "internal detail"})
    ctx = await make_ctx("priya", transport)

    result = await registry.execute("get_shipment", ctx, {"order_number": "ORD-1001"})

    error = expect_error(result, code, retryable=retryable)
    assert "internal detail" not in error.message  # provider text is not passed through


async def test_unexpected_failure_in_a_real_tool_is_generic(
    make_ctx: MakeCtx, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("connection to db.internal:5432 refused")

    monkeypatch.setattr("support_agent.services.eligibility.refunds.sum_by_status", broken)
    ctx = await make_ctx("priya")

    result = await registry.execute("get_refundable_balance", ctx, {"order_number": "ORD-1001"})

    error = expect_error(result, "INTERNAL_ERROR")
    assert "db.internal" not in error.message


# ---------------------------------------------------------------- schema export


def test_registry_exports_schemas_for_every_tool() -> None:
    schemas = {schema["name"]: schema for schema in registry.export_schemas()}

    assert set(schemas) == {*READ_TOOLS, "search_policy"}
    for schema in schemas.values():
        params = schema["parameters"]
        assert schema["description"]
        assert params["additionalProperties"] is False
        assert "customer_id" not in str(params)  # no customer id, ever
        assert schema["output_schema"]["type"] == "object"
    for name in READ_TOOLS:
        params = schemas[name]["parameters"]
        assert list(params["properties"]) == ["order_number"]
        assert params["required"] == ["order_number"]
    search = schemas["search_policy"]["parameters"]
    assert list(search["properties"]) == ["query", "order_number"]
    assert search["required"] == ["query"]  # order_number is optional
    assert search["properties"]["query"]["maxLength"] == 500
