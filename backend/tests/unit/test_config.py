import pytest
from pydantic import ValidationError

from support_agent.core.config import Settings

VALID_URL = "postgresql+asyncpg://user:secret@localhost:5432/support_agent"


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings(_env_file=None)  # ignore any local .env so the test is repeatable

    assert settings.database_url.get_secret_value() == VALID_URL
    assert settings.log_level == "DEBUG"
    assert "secret" not in repr(settings)  # the password is never shown


def test_settings_reject_non_async_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@localhost:5432/db")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
