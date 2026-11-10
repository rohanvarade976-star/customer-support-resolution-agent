"""Settings for the mock payment provider.

The mock is meant to behave like a separate third-party service, so it has its own
settings and never imports from `support_agent`. It reads the same repo-root `.env`.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> payments -> mock_providers -> src -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]


class PaymentsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "test", "production"] = "local"
    # Refunds are stored in the "mock_payments" schema of this database.
    database_url: SecretStr

    # How long a refund stays in each state before moving on.
    mock_payments_submitted_seconds: float = Field(default=2.0, ge=0)
    mock_payments_processing_seconds: float = Field(default=5.0, ge=0)
