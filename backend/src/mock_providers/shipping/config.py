"""Settings for the mock shipping provider."""

from pathlib import Path

from mock_providers.common.config import BACKEND_DIR, ProviderSettings


class ShippingSettings(ProviderSettings):
    # Read once at startup. Times inside are "days ago", so the data never goes stale.
    mock_shipping_data_file: Path = BACKEND_DIR / "data" / "mock_shipping" / "shipments.json"
