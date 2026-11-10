"""Request and response models for the mock payment provider's HTTP API."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


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


class ErrorBody(BaseModel):
    """Every error the mock returns has this shape (matches the PRD failure format)."""

    success: Literal[False] = False
    error_code: str
    retryable: bool
    message: str


class FaultMode(StrEnum):
    NONE = "none"
    SLOW = "slow"  # wait delay_ms, then handle normally
    ERROR = "error"  # 500 without touching the database
    DROP_RESPONSE = "drop_response"  # do the work, then reply 504 as if the reply was lost
    DECLINE = "decline"  # newly created refunds end as "failed"


class FaultConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: FaultMode
    # How many upcoming /refunds requests the fault applies to.
    count: StrictInt = Field(default=1, ge=0)
    # Only used by "slow".
    delay_ms: StrictInt = Field(default=0, ge=0, le=60_000)

    @model_validator(mode="after")
    def _slow_needs_delay(self) -> "FaultConfig":
        if self.mode is FaultMode.SLOW and self.delay_ms == 0:
            raise ValueError("mode 'slow' needs delay_ms > 0")
        return self


class FaultState(BaseModel):
    mode: FaultMode
    remaining: int
    delay_ms: int
