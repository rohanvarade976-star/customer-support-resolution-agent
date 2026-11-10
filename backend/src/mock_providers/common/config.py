"""Base settings for every mock provider.

The mocks behave like separate third-party services, so they have their own settings and
never import from `support_agent`. They read the same repo-root `.env`.
"""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> common -> mock_providers -> src -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND_DIR = REPO_ROOT / "backend"


class ProviderSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # In "production" the /_admin/faults routes are not registered at all.
    app_env: Literal["local", "test", "production"] = "local"
