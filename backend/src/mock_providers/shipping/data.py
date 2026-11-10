"""Loads the shipping data file and turns its "days ago" values into real timestamps.

`resolve_shipment` is a pure function: given a record and "now", it returns the API
response. Nothing is stored with absolute dates, so the demo data never goes stale.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from mock_providers.shipping.schemas import (
    ScanEvent,
    ScanEventRecord,
    ShipmentDataFile,
    ShipmentRecord,
    ShipmentResponse,
    ShipmentStatus,
)


def load_shipments(path: Path) -> dict[str, ShipmentRecord]:
    """Read and validate the data file. Returns shipments keyed by order reference."""
    data = ShipmentDataFile.model_validate_json(path.read_text(encoding="utf-8"))
    by_order = {shipment.order_reference: shipment for shipment in data.shipments}
    if len(by_order) != len(data.shipments):
        raise ValueError(f"{path}: duplicate order_reference")
    return by_order


def _event_time(event: ScanEventRecord, now: datetime) -> datetime:
    day = (now - timedelta(days=event.days_ago)).date()
    return datetime.combine(day, event.time, tzinfo=UTC)


def resolve_shipment(record: ShipmentRecord, now: datetime) -> ShipmentResponse:
    events = sorted(
        (
            ScanEvent(
                time=_event_time(event, now),
                location=event.location,
                description=event.description,
            )
            for event in record.events
        ),
        key=lambda event: event.time,
        reverse=True,  # newest first, like a courier tracking page
    )
    delivered = record.status is ShipmentStatus.DELIVERED
    return ShipmentResponse(
        order_reference=record.order_reference,
        status=record.status,
        carrier=record.carrier,
        tracking_number=record.tracking_number,
        shipped_at=events[-1].time,  # the first scan is the pickup
        expected_delivery=(now - timedelta(days=record.expected_delivery_days_ago)).date(),
        delivered_at=events[0].time if delivered else None,
        events=events,
    )
