"""CORS (ADR-019): only the configured origins, never "*".

Preflights are answered by the middleware before any route runs, so no database is needed.
Settings are built from environment variables with the local .env ignored, so the result
does not depend on anyone's own configuration.
"""

import httpx
import pytest
from pydantic import ValidationError

from support_agent import main
from support_agent.core.config import Settings

ALLOWED = "http://localhost:5173"
STRANGER = "https://evil.example"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("JWT_SECRET", "a-long-secret-" + "x" * 30)
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    return monkeypatch


async def _preflight(env: pytest.MonkeyPatch, origin: str) -> httpx.Response:
    settings = Settings(_env_file=None)
    env.setattr(main, "get_settings", lambda: settings)
    transport = httpx.ASGITransport(app=main.create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.options(
            "/me/orders",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )


def test_default_origins_are_the_local_frontends(env: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    assert settings.cors_allowed_origins == ["http://localhost:5173", "http://localhost:3000"]


async def test_allowed_origin_gets_cors_headers(env: pytest.MonkeyPatch) -> None:
    response = await _preflight(env, ALLOWED)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in response.headers  # no cookies


async def test_other_origin_gets_no_cors_headers(env: pytest.MonkeyPatch) -> None:
    response = await _preflight(env, STRANGER)
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_origins_are_read_comma_separated(env: pytest.MonkeyPatch) -> None:
    env.setenv("CORS_ALLOWED_ORIGINS", "https://a.example, https://b.example")
    settings = Settings(_env_file=None)
    assert settings.cors_allowed_origins == ["https://a.example", "https://b.example"]


@pytest.mark.parametrize("value", ["*", "https://*.example", "https://a.example/", "a.example"])
def test_wildcards_and_malformed_origins_are_refused(env: pytest.MonkeyPatch, value: str) -> None:
    env.setenv("CORS_ALLOWED_ORIGINS", value)
    with pytest.raises(ValidationError, match="CORS_ALLOWED_ORIGINS"):
        Settings(_env_file=None)
