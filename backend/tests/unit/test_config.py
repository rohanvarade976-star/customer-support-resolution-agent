import pytest
from pydantic import ValidationError

from support_agent.core.config import Settings

VALID_URL = "postgresql+asyncpg://user:secret@localhost:5432/support_agent"
VALID_JWT_SECRET = "a-long-secret-" + "x" * 30


@pytest.fixture
def valid_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    monkeypatch.setenv("JWT_SECRET", VALID_JWT_SECRET)
    return monkeypatch


def test_settings_load_from_environment(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings(_env_file=None)  # ignore any local .env so the test is repeatable

    assert settings.database_url.get_secret_value() == VALID_URL
    assert settings.log_level == "DEBUG"
    assert settings.access_token_minutes == 60  # default
    assert "user:secret" not in repr(settings)  # the database password is never shown
    assert VALID_JWT_SECRET not in repr(settings)  # nor the signing key


def test_settings_reject_non_async_database_url(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv("DATABASE_URL", "postgresql://user:secret@localhost:5432/db")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_reject_short_jwt_secret(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv("JWT_SECRET", "too-short")

    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None)


def test_settings_require_jwt_secret(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.delenv("JWT_SECRET")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_reject_absurd_token_lifetime(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv("ACCESS_TOKEN_MINUTES", "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
