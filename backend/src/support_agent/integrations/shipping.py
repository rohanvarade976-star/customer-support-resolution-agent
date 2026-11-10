"""Client for the shipping provider's tracking API (read-only, so always retried)."""

from datetime import date, datetime
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from support_agent.core.config import Settings
from support_agent.integrations.http import BaseProviderClient, ProviderHttp

PROVIDER = "shipping"


class ScanEvent(BaseModel):
    time: datetime
    location: str
    description: str


class Shipment(BaseModel):
    """A shipment as the carrier reports it. `status` is the carrier's own word."""

    order_reference: str
    status: str
    carrier: str
    tracking_number: str
    shipped_at: datetime
    expected_delivery: date
    delivered_at: datetime | None
    events: list[ScanEvent]  # newest first


class ShippingClient(BaseProviderClient):
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(ProviderHttp(PROVIDER, settings.shipping_base_url, settings, transport))

    async def get_shipment(self, order_reference: str) -> Shipment:
        """Latest tracking for an order. Raises ResourceNotFoundError if there is none."""
        response = await self._http.request("GET", f"/shipments/{quote(order_reference, safe='')}")
        return Shipment.model_validate_json(response.content)
