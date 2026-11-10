"""Application settings, loaded once from environment variables and the `.env` file.

Every other module reads config through `get_settings()`, never from `os.environ`.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> core -> support_agent -> src -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Local dev reads the repo-root .env (shared with docker compose).
        # In CI and production the file is absent and real env vars are used.
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        # .env also holds values for docker compose (POSTGRES_*); ignore those here.
        extra="ignore",
    )

    app_env: Literal["local", "test", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Required: no default, so a missing value fails at startup instead of later.
    # SecretStr hides the password if settings are ever printed or logged.
    database_url: SecretStr
    # Separate database used only by integration tests (they wipe it). Optional:
    # without it, integration tests are skipped locally (and fail in CI).
    test_database_url: SecretStr | None = None

    # External providers (the mocks locally). Used only by `integrations/`.
    payments_base_url: str = "http://localhost:8001"
    shipping_base_url: str = "http://localhost:8002"
    ticketing_base_url: str = "http://localhost:8003"
    provider_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=60)
    provider_read_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    # Total tries for retried calls (1 = no retry). Refund creation always makes one try.
    provider_max_attempts: int = Field(default=3, ge=1, le=10)
    # First retry waits up to this long; each later retry doubles the limit.
    provider_backoff_base_seconds: float = Field(default=0.2, ge=0, le=10)

    @field_validator("payments_base_url", "shipping_base_url", "ticketing_base_url")
    @classmethod
    def _require_http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("provider base URLs must start with 'http://' or 'https://'")
        return value

    @field_validator("database_url", "test_database_url")
    @classmethod
    def _require_async_driver(cls, value: SecretStr | None) -> SecretStr | None:
        # The whole app is async, so the URL must use the asyncpg driver.
        if value is not None and not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("database URLs must start with 'postgresql+asyncpg://'")
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the settings, created once and reused (cached)."""
    return Settings()  # values come from the environment / .env
