"""Fake read tools for tests: canned results, no database, no network.

`FakeTools.registry` holds the six investigator tools plus two tools an investigator must
never be able to use (`search_policy`, `execute_approved_refund`), so tests can prove the
allowlist works. Every executed call is kept in `calls`, with the customer id the tool
ran as, so tests can check what ran, in what order, and for whom.

The order number ORD-9999 behaves like a missing or foreign order: the tool fails with
ORDER_NOT_FOUND, exactly as the real services do.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from pydantic import BaseModel, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.integrations.shipping import ShippingClient
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.tools.base import Tool, ToolContext, ToolRegistry
from support_agent.tools.read_tools import OrderInput

MISSING_ORDER = "ORD-9999"

FAKE_TOOL_NAMES = (
    "get_order",
    "get_return_status",
    "get_payment_status",
    "get_shipment",
    "get_refundable_balance",
    "check_refund_eligibility",
    "search_policy",  # a real tool the investigator must not have
    "execute_approved_refund",  # a write tool no agent may have
)


class FakeRecord(BaseModel):
    tool: str
    order_number: str
    detail: str


@dataclass(frozen=True)
class FakeCall:
    tool: str
    order_number: str
    customer_id: UUID  # who the tool ran as (comes from the ToolContext)


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[FakeCall] = []
        self.detail = "canned result"  # the text in every result; tests may change it
        self.registry = ToolRegistry()
        for name in FAKE_TOOL_NAMES:
            self.registry.register(
                Tool(name, f"Fake {name}.", OrderInput, FakeRecord, self._handler(name))
            )

    def _handler(self, name: str) -> Callable[[ToolContext, OrderInput], Awaitable[FakeRecord]]:
        async def handle(ctx: ToolContext, args: OrderInput) -> FakeRecord:
            self.calls.append(FakeCall(name, args.order_number, ctx.customer_id))
            if args.order_number == MISSING_ORDER:
                raise AppError(404, "ORDER_NOT_FOUND", "Order not found.")
            return FakeRecord(tool=name, order_number=args.order_number, detail=self.detail)

        return handle


def fake_tool_context(customer_id: UUID | None = None) -> ToolContext:
    """A context that works with FakeTools. The session and shipping client are never used."""
    settings = Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+asyncpg://u:p@localhost/unused"),
        jwt_secret=SecretStr("x" * 32),
    )
    return ToolContext(
        session=MagicMock(spec=AsyncSession),
        customer_id=customer_id or uuid4(),
        settings=settings,
        shipping_client=MagicMock(spec=ShippingClient),
        embedder=FakeEmbedder(),
        clock=lambda: datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
    )
