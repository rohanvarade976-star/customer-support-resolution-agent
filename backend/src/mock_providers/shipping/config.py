"""Settings for the mock shipping provider."""

from datetime import date
from pathlib import Path

from pydantic import field_validator

from mock_providers.common.config import BACKEND_DIR, ProviderSettings


class ShippingSettings(ProviderSettings):
    # Read once at startup. Times inside are "days ago", so the data never goes stale.
    mock_shipping_data_file: Path = BACKEND_DIR / "data" / "mock_shipping" / "shipments.json"

    # Pins "N days ago" to noon UTC on this date, the same setting the database seed uses
    # (see support_agent/core/demo_anchor.py). Unset: the moment the provider started.
    demo_anchor_date: date | None = None

    @field_validator("demo_anchor_date", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value
