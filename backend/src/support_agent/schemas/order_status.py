"""Result models for the read tools. Internal ids, idempotency keys and provider
references are left out on purpose."""

from datetime import datetime

from pydantic import BaseModel

from support_agent.domain.enums import InspectionStatus, RefundOperationStatus, ReturnStatus


class ReturnStatusResponse(BaseModel):
    """The latest non-cancelled return. All fields except order_number are None if there is none."""

    order_number: str
    return_status: ReturnStatus | None
    requested_at: datetime | None
    received_at: datetime | None
    inspection_status: InspectionStatus | None


class RefundSummary(BaseModel):
    status: RefundOperationStatus
    amount_minor: int  # paise
    created_at: datetime


class PaymentStatusResponse(BaseModel):
    order_number: str
    currency: str
    paid_amount_minor: int  # paise
    succeeded_refunds_minor: int
    in_progress_refunds_minor: int
    refunds: list[RefundSummary]


class RefundableBalanceResponse(BaseModel):
    order_number: str
    currency: str
    paid_amount_minor: int
    succeeded_refunds_minor: int
    in_progress_refunds_minor: int
    refundable_balance_minor: int
