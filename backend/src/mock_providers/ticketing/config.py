"""Settings for the mock ticketing provider."""

from pydantic import SecretStr

from mock_providers.common.config import ProviderSettings


class TicketingSettings(ProviderSettings):
    # Tickets and notes are stored in the "mock_ticketing" schema of this database.
    database_url: SecretStr
