"""The signed-in customer's own orders. Customer-only: reviewers get 403."""

from fastapi import APIRouter

from support_agent.api.deps import CustomerDep, SessionDep
from support_agent.schemas.orders import OrderResponse
from support_agent.services import orders as orders_service

router = APIRouter(tags=["orders"])


@router.get("/me/orders")
async def list_my_orders(principal: CustomerDep, session: SessionDep) -> list[OrderResponse]:
    return await orders_service.list_my_orders(session, principal)


@router.get("/me/orders/{order_number}")
async def get_my_order(
    order_number: str, principal: CustomerDep, session: SessionDep
) -> OrderResponse:
    return await orders_service.get_my_order(session, principal, order_number)
