"""Request and response models for the mock payment provider's HTTP API."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class ProviderRefundStatus(StrEnum):
    """The provider's own status vocabulary (not our app's RefundOperationStatus)."""

    SUBMITTED = "submitted"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FinalOutcome(StrEnum):
    """Where a refund ends up once processing is over. Decided when it is created."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RefundRequest(BaseModel):
    # extra="forbid": unknown fields are an error, not silently ignored.
    model_config = ConfigDict(extra="forbid")

    order_reference: str = Field(min_length=1, max_length=100)
    # StrictInt rejects floats like 499.0 and strings like "49900". Money is paise.
    amount_minor: StrictInt = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reason: str = Field(min_length=1, max_length=500)


class RefundResponse(BaseModel):
    provider_reference: str
    idempotency_key: str
    order_reference: str
    amount_minor: int
    currency: str
    reason: str
    status: ProviderRefundStatus
    created_at: datetime
