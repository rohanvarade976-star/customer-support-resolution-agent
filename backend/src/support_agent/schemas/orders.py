"""What a customer sees about their orders. Internal ids and fields are left out on purpose."""

from datetime import datetime

from pydantic import BaseModel

from support_agent.domain.enums import OrderStatus, ReturnStatus


class OrderItemResponse(BaseModel):
    product_id: str
    product_name: str
    quantity: int
    paid_amount_minor: int  # paise, for the whole line


class ReturnResponse(BaseModel):
    status: ReturnStatus
    requested_at: datetime
    received_at: datetime | None


class OrderResponse(BaseModel):
    order_number: str
    status: OrderStatus
    currency: str
    paid_amount_minor: int  # paise
    placed_at: datetime
    delivered_at: datetime | None
    items: list[OrderItemResponse]
    returns: list[ReturnResponse]
