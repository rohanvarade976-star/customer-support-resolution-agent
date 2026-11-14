"""API rate limits (ADR-017): login brute force and model-cost abuse, counted in Postgres.

Limits are lowered per test through the settings override so a few requests reach them. The
clock is movable, so a window can be reset without waiting. Assertions look at the status,
the Retry-After header, the structured error body and the stored counters.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.api.deps import get_clock
from support_agent.core.config import Settings, get_settings
from support_agent.core.errors import AppError
from support_agent.db.models import RateLimitCounter
from support_agent.db.seed import load_scenarios, seed
from support_agent.services import rate_limits

# 12:05 inside the 12:00-12:15 login window, so a refused login has 600 seconds to wait.
NOW = datetime(2026, 11, 10, 12, 5, tzinfo=UTC)
LOGIN_WINDOW = 900


class MovableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(app: FastAPI) -> MovableClock:
    moving = MovableClock(NOW)
    app.dependency_overrides[get_clock] = lambda: moving
    return moving


@pytest.fixture
def limits(app: FastAPI) -> dict[str, Any]:
    """Change a limit with `limits["rate_limit_login_per_email"] = 2`, before the requests."""
    overrides: dict[str, Any] = {
        "rate_limit_login_per_email": 3,
        "rate_limit_login_per_ip": 50,
        "rate_limit_login_window_seconds": LOGIN_WINDOW,
        "rate_limit_run_starts_per_hour": 2,
        "rate_limit_replies_per_hour": 1,
    }
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(update=overrides)
    return overrides


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


def client_at(app: FastAPI, ip: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(ip, 5000))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def try_login(
    http: httpx.AsyncClient,
    email: str,
    password: str = "wrong-password",  # noqa: S107 (test value)
) -> httpx.Response:
    return await http.post("/auth/login", json={"email": email, "password": password})


def assert_rate_limited(response: httpx.Response, scope: str) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    assert response.status_code == 429
    assert body["success"] is False
    assert body["error_code"] == "RATE_LIMITED"
    assert body["details"]["scope"] == scope
    assert response.headers["Retry-After"] == str(body["details"]["retry_after_seconds"])
    return body


# ---------------------------------------------------------------- login: per email


async def test_login_attempts_per_email_are_limited_even_with_the_right_password(
    app: FastAPI, limits: dict[str, Any], clock: MovableClock, demo_password: str
) -> None:
    async with client_at(app, "10.0.0.1") as http:
        for _ in range(3):
            assert (await try_login(http, "priya@example.com")).status_code == 401

        refused = await try_login(http, "priya@example.com", demo_password)

        body = assert_rate_limited(refused, "login_email")
        assert body["details"]["limit"] == 3
        assert body["details"]["window_seconds"] == LOGIN_WINDOW
        assert body["details"]["retry_after_seconds"] == 600  # 12:05 -> the window ends 12:15
        assert "access_token" not in refused.text


async def test_the_email_is_matched_ignoring_case_and_spaces(
    app: FastAPI, limits: dict[str, Any], clock: MovableClock
) -> None:
    async with client_at(app, "10.0.0.1") as http:
        for email in ("priya@example.com", " Priya@Example.com", "PRIYA@EXAMPLE.COM"):
            await try_login(http, email)

        assert_rate_limited(await try_login(http, "priya@example.com"), "login_email")


async def test_another_email_is_not_affected(
    app: FastAPI, limits: dict[str, Any], clock: MovableClock, demo_password: str
) -> None:
    async with client_at(app, "10.0.0.1") as http:
        for _ in range(4):
            await try_login(http, "priya@example.com")

        ok = await try_login(http, "arjun@example.com", demo_password)

        assert ok.status_code == 200


async def test_the_limit_resets_when_the_window_ends(
    app: FastAPI, limits: dict[str, Any], clock: MovableClock, demo_password: str
) -> None:
    async with client_at(app, "10.0.0.1") as http:
        for _ in range(4):
            await try_login(http, "priya@example.com")
        assert_rate_limited(await try_login(http, "priya@example.com"), "login_email")

        clock.now = NOW + timedelta(seconds=600)  # 12:15, the next window
        assert (await try_login(http, "priya@example.com", demo_password)).status_code == 200


# ---------------------------------------------------------------- login: per IP


async def test_login_attempts_per_ip_are_limited_across_different_emails(
    app: FastAPI, limits: dict[str, Any], clock: MovableClock, demo_password: str
) -> None:
    limits["rate_limit_login_per_ip"] = 3
    async with client_at(app, "10.0.0.7") as noisy, client_at(app, "10.0.0.8") as quiet:
        for n in range(3):
            assert (await try_login(noisy, f"nobody{n}@example.com")).status_code == 401

        refused = await try_login(noisy, "priya@example.com", demo_password)

        assert_rate_limited(refused, "login_ip")
        # A different client address is counted on its own.
        assert (await try_login(quiet, "priya@example.com", demo_password)).status_code == 200


# ---------------------------------------------------------------- customer actions


async def test_run_starts_per_customer_are_limited(
    client: httpx.AsyncClient,
    limits: dict[str, Any],
    clock: MovableClock,
    auth_headers: Any,
) -> None:
    priya = await auth_headers("priya@example.com")
    arjun = await auth_headers("arjun@example.com")
    ticket_ids = []
    for n in range(3):
        created = await client.post("/tickets", json={"message": f"Problem {n}"}, headers=priya)
        ticket_ids.append(created.json()["id"])

    first = await client.post(f"/tickets/{ticket_ids[0]}/runs", headers=priya)
    second = await client.post(f"/tickets/{ticket_ids[1]}/runs", headers=priya)
    third = await client.post(f"/tickets/{ticket_ids[2]}/runs", headers=priya)

    assert (first.status_code, second.status_code) == (202, 202)
    assert_rate_limited(third, "run_start")
    # Nothing was queued for the refused request.
    assert (await client.get(f"/tickets/{ticket_ids[2]}", headers=priya)).json()["status"] == "open"
    # Another customer has their own allowance.
    other = await client.post("/tickets", json={"message": "Hello"}, headers=arjun)
    assert (
        await client.post(f"/tickets/{other.json()['id']}/runs", headers=arjun)
    ).status_code == 202


async def test_customer_replies_per_customer_are_limited_and_the_hour_resets(
    client: httpx.AsyncClient,
    limits: dict[str, Any],
    clock: MovableClock,
    auth_headers: Any,
) -> None:
    priya = await auth_headers("priya@example.com")
    unknown_run = f"/runs/{uuid4()}/messages"

    first = await client.post(unknown_run, json={"text": "hello"}, headers=priya)
    second = await client.post(unknown_run, json={"text": "hello"}, headers=priya)

    assert first.status_code == 404  # allowed through to the (failing) request
    assert_rate_limited(second, "customer_reply")

    clock.now = NOW + timedelta(hours=1)
    assert (await client.post(unknown_run, json={"text": "hi"}, headers=priya)).status_code == 404


async def test_staff_requests_are_refused_before_they_are_counted(
    client: httpx.AsyncClient, limits: dict[str, Any], clock: MovableClock, auth_headers: Any
) -> None:
    reviewer = await auth_headers("anita@example.com")
    for _ in range(3):
        response = await client.post(
            f"/runs/{uuid4()}/messages", json={"text": "hi"}, headers=reviewer
        )
        assert response.status_code == 403  # customers only, never 429


async def test_limits_can_be_switched_off(
    app: FastAPI, limits: dict[str, Any], clock: MovableClock
) -> None:
    limits["rate_limit_enabled"] = False
    async with client_at(app, "10.0.0.1") as http:
        for _ in range(6):
            assert (await try_login(http, "priya@example.com")).status_code == 401


# ---------------------------------------------------------------- what is stored and logged


async def test_counters_hold_no_email_ip_or_password_and_nothing_sensitive_is_logged(
    app: FastAPI,
    limits: dict[str, Any],
    clock: MovableClock,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    password = "hunter2-not-a-real-password"  # noqa: S105 (a test value, not a secret)
    async with client_at(app, "10.9.8.7") as http:
        for _ in range(4):
            await try_login(http, "priya@example.com", password)

    rows = (await session.scalars(select(RateLimitCounter))).all()
    stored = " ".join(f"{row.bucket_key}" for row in rows)
    assert {row.bucket_key.split(":")[0] for row in rows} == {"login_ip", "login_email"}
    assert all(re.fullmatch(r"[a-z_]+:[0-9a-f]{64}", row.bucket_key) for row in rows)
    for sensitive in ("priya", "example.com", "10.9.8.7", password):
        assert sensitive not in stored
        assert sensitive not in caplog.text
    assert any(record.getMessage() == "rate_limited" for record in caplog.records)


async def test_expired_counters_are_deleted(
    app: FastAPI,
    limits: dict[str, Any],
    clock: MovableClock,
    session: AsyncSession,
) -> None:
    async with client_at(app, "10.0.0.1") as http:
        await try_login(http, "priya@example.com")
        clock.now = NOW + timedelta(hours=2)
        await try_login(http, "arjun@example.com")

    windows = (await session.scalars(select(RateLimitCounter.window_start))).all()
    assert all(window >= clock.now - timedelta(seconds=LOGIN_WINDOW) for window in windows)
    assert len(windows) == 2  # only the new window's ip and email counters remain


# ---------------------------------------------------------------- across processes


@pytest.fixture
async def committed_counters(
    committed_sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield committed_sessions
    async with committed_sessions() as cleanup:
        await cleanup.execute(delete(RateLimitCounter))
        await cleanup.commit()


async def test_concurrent_requests_on_separate_connections_share_one_count(
    committed_counters: async_sessionmaker[AsyncSession],
) -> None:
    """Twenty simultaneous requests, each on its own connection (as separate processes would
    be): exactly `limit` get through and the rest are refused, and no increment is lost."""
    settings: Settings = get_settings().model_copy(update={"rate_limit_enabled": True})

    async def one_request() -> bool:
        async with committed_counters() as db:
            try:
                await rate_limits.hit(
                    db,
                    settings,
                    scope=rate_limits.Scope.RUN_START,
                    identity="the-same-customer",
                    limit=8,
                    window_seconds=3600,
                    now=NOW,
                )
            except AppError as exc:
                assert exc.status_code == 429
                return False
            return True

    results = await asyncio.gather(*(one_request() for _ in range(20)))

    assert results.count(True) == 8
    async with committed_counters() as check:
        total = await check.scalar(select(func.max(RateLimitCounter.count)))
    assert total == 20
