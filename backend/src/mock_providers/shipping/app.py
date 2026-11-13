"""Mock shipping provider: read-only tracking lookups from a JSON file.

Shipments are loaded into memory once when the app is built. Faults (slow, error,
drop_response) can be switched on to test how our app handles a flaky carrier API.

Run locally (from backend/):
    uv run uvicorn mock_providers.shipping.app:create_app --factory --port 8002
"""

from fastapi import FastAPI

from mock_providers.common.admin import register_admin_routes
from mock_providers.common.clock import Clock, anchored_clock, utc_now
from mock_providers.common.errors import ProviderError, register_error_handlers
from mock_providers.common.faults import FaultInjector, install_fault_middleware
from mock_providers.shipping.config import ShippingSettings
from mock_providers.shipping.data import load_shipments, resolve_shipment
from mock_providers.shipping.schemas import ShipmentResponse


def create_app(settings: ShippingSettings | None = None, clock: Clock | None = None) -> FastAPI:
    """Build the app. Tests pass their own settings and a fake clock."""
    settings = settings or ShippingSettings()
    # Frozen at startup (or at DEMO_ANCHOR_DATE) so dates match the database seed.
    clock = clock or anchored_clock(settings.demo_anchor_date, utc_now())
    shipments = load_shipments(settings.mock_shipping_data_file)  # fails fast on bad data
    faults = FaultInjector()

    app = FastAPI(title="Mock shipping provider")
    install_fault_middleware(app, faults, "/shipments")
    register_error_handlers(app)
    register_admin_routes(app, faults, settings.app_env)

    @app.get("/shipments/{order_reference}", response_model=ShipmentResponse)
    async def get_shipment(order_reference: str) -> ShipmentResponse:
        record = shipments.get(order_reference)
        if record is None:
            raise ProviderError(
                404, "SHIPMENT_NOT_FOUND", "No shipment for this order.", retryable=False
            )
        return resolve_shipment(record, clock())

    return app
