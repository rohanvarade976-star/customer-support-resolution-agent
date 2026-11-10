"""Settings for the mock payment provider."""

from pydantic import Field, SecretStr

from mock_providers.common.config import ProviderSettings


class PaymentsSettings(ProviderSettings):
    # Refunds are stored in the "mock_payments" schema of this database.
    database_url: SecretStr

    # How long a refund stays in each state before moving on.
    mock_payments_submitted_seconds: float = Field(default=2.0, ge=0)
    mock_payments_processing_seconds: float = Field(default=5.0, ge=0)
