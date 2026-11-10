"""Models for the shipping data file and the mock shipping provider's HTTP API."""

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ShipmentStatus(StrEnum):
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"


# ---------------------------------------------------------------- data file (relative times)


class ScanEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days_ago: int = Field(ge=1)  # at least 1, so a scan is never in the future
    time: dt.time  # time of day, UTC
    location: str
    description: str


class ShipmentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_reference: str
    carrier: str
    tracking_number: str
    status: ShipmentStatus
    expected_delivery_days_ago: int  # negative means the expected date is in the future
    events: list[ScanEventRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def _delivered_ends_with_delivery_scan(self) -> "ShipmentRecord":
        last = max(self.events, key=lambda event: (-event.days_ago, event.time))
        is_delivered_scan = last.description == "Delivered"
        if is_delivered_scan != (self.status is ShipmentStatus.DELIVERED):
            raise ValueError(f"{self.order_reference}: status and last scan disagree")
        return self


class ShipmentDataFile(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    comment: str = Field(default="", alias="_comment")
    shipments: list[ShipmentRecord]


# ---------------------------------------------------------------- HTTP API (absolute times)


class ScanEvent(BaseModel):
    time: dt.datetime
    location: str
    description: str


class ShipmentResponse(BaseModel):
    order_reference: str
    status: ShipmentStatus
    carrier: str
    tracking_number: str
    shipped_at: dt.datetime
    expected_delivery: dt.date
    delivered_at: dt.datetime | None
    events: list[ScanEvent]  # newest first
