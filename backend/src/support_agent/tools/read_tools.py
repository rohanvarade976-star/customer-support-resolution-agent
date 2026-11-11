"""The six read-only tools. Each takes an order number and returns a typed result.

Handlers are thin: they call a service with `ctx.customer_id`. Ownership is enforced
inside the services, so another customer's order returns ORDER_NOT_FOUND.
"""

from pydantic import BaseModel, ConfigDict, Field

from support_agent.domain.eligibility import EligibilityDecision
from support_agent.integrations.errors import ResourceNotFoundError
from support_agent.integrations.shipping import Shipment
from support_agent.schemas.order_status import (
    PaymentStatusResponse,
    RefundableBalanceResponse,
    ReturnStatusResponse,
)
from support_agent.schemas.orders import OrderResponse
from support_agent.services import eligibility, order_status
from support_agent.services import orders as order_service
from support_agent.tools.base import Tool, ToolContext, ToolFailureError, ToolRegistry


class OrderInput(BaseModel):
    """The only argument any read tool takes. There is deliberately no customer field."""

    model_config = ConfigDict(extra="forbid")

    order_number: str = Field(
        pattern=r"^ORD-[0-9]+$",
        max_length=20,
        description="The order number, for example ORD-1001.",
    )


async def _get_order(ctx: ToolContext, args: OrderInput) -> OrderResponse:
    return await order_service.get_order_for_customer(
        ctx.session, ctx.customer_id, args.order_number
    )


async def _get_return_status(ctx: ToolContext, args: OrderInput) -> ReturnStatusResponse:
    return await order_status.get_return_status(ctx.session, ctx.customer_id, args.order_number)


async def _get_payment_status(ctx: ToolContext, args: OrderInput) -> PaymentStatusResponse:
    return await order_status.get_payment_status(ctx.session, ctx.customer_id, args.order_number)


async def _get_shipment(ctx: ToolContext, args: OrderInput) -> Shipment:
    try:
        return await order_status.get_shipment(
            ctx.session, ctx.shipping_client, ctx.customer_id, args.order_number
        )
    except ResourceNotFoundError as exc:
        raise ToolFailureError("SHIPMENT_NOT_FOUND", "No shipment exists for this order.") from exc


async def _get_refundable_balance(ctx: ToolContext, args: OrderInput) -> RefundableBalanceResponse:
    return await eligibility.get_refundable_balance(ctx.session, ctx.customer_id, args.order_number)


async def _check_refund_eligibility(ctx: ToolContext, args: OrderInput) -> EligibilityDecision:
    return await eligibility.check_refund_eligibility(
        ctx.session,
        ctx.customer_id,
        args.order_number,
        ctx.clock(),
        ctx.settings.refund_window_days,
    )


def build_read_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="get_order",
            description="Get the customer's order: status, items, amounts and dates.",
            input_model=OrderInput,
            output_model=OrderResponse,
            handler=_get_order,
        )
    )
    registry.register(
        Tool(
            name="get_return_status",
            description="Get the latest return for the order: status, received date, inspection.",
            input_model=OrderInput,
            output_model=ReturnStatusResponse,
            handler=_get_return_status,
        )
    )
    registry.register(
        Tool(
            name="get_payment_status",
            description="Get the amount paid and every refund made or in progress for the order.",
            input_model=OrderInput,
            output_model=PaymentStatusResponse,
            handler=_get_payment_status,
        )
    )
    registry.register(
        Tool(
            name="get_shipment",
            description="Get the carrier's latest tracking status and scan events for the order.",
            input_model=OrderInput,
            output_model=Shipment,
            handler=_get_shipment,
        )
    )
    registry.register(
        Tool(
            name="get_refundable_balance",
            description="Get how much of the order can still be refunded (paid minus refunds).",
            input_model=OrderInput,
            output_model=RefundableBalanceResponse,
            handler=_get_refundable_balance,
        )
    )
    registry.register(
        Tool(
            name="check_refund_eligibility",
            description=(
                "Check, with fixed rules, whether the order can be refunded and list every "
                "reason it cannot. Only this result is authoritative; never guess eligibility."
            ),
            input_model=OrderInput,
            output_model=EligibilityDecision,
            handler=_check_refund_eligibility,
        )
    )
    return registry
