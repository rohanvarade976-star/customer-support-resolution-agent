"""Order reads. The customer id always comes from the Principal.

Asking for an order that is not yours looks exactly like asking for one that does
not exist: a 404, never a 403, so existence is not revealed (FR-2).
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.errors import AppError
from support_agent.db.models import Order
from support_agent.repositories import orders
from support_agent.schemas.auth import Principal
from support_agent.schemas.orders import OrderItemResponse, OrderResponse, ReturnResponse


def order_not_found() -> AppError:
    return AppError(404, "ORDER_NOT_FOUND", "Order not found.")


def _to_response(order: Order) -> OrderResponse:
    return OrderResponse(
        order_number=order.order_number,
        status=order.status,
        currency=order.currency,
        paid_amount_minor=order.paid_amount_minor,
        placed_at=order.placed_at,
        delivered_at=order.delivered_at,
        items=[
            OrderItemResponse(
                product_id=item.product_id,
                product_name=item.product_name,
                quantity=item.quantity,
                paid_amount_minor=item.paid_amount_minor,
            )
            for item in order.items
        ],
        returns=[
            ReturnResponse(
                status=ret.status, requested_at=ret.created_at, received_at=ret.received_at
            )
            for ret in order.returns
        ],
    )


async def list_my_orders(session: AsyncSession, principal: Principal) -> list[OrderResponse]:
    found = await orders.list_for_customer(session, principal.id)
    return [_to_response(order) for order in found]


async def get_order_for_customer(
    session: AsyncSession, customer_id: UUID, order_number: str
) -> OrderResponse:
    """Shared by the API (customer id from the Principal) and the read tools (from the run)."""
    order = await orders.get_for_customer(session, customer_id, order_number)
    if order is None:
        raise order_not_found()
    return _to_response(order)


async def get_my_order(
    session: AsyncSession, principal: Principal, order_number: str
) -> OrderResponse:
    return await get_order_for_customer(session, principal.id, order_number)
