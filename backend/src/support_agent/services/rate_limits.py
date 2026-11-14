"""API rate limits, kept in Postgres so they hold across processes (ADR-017).

Fixed windows: each (scope, client) has one counter per window, and the request that takes
it past the limit is refused with 429 and a Retry-After header. A client can therefore make
up to twice the limit around a window boundary; that is the price of one atomic statement.

What is counted:
- login attempts, per email and per client IP (brute force). Every attempt counts, wrong
  password or not, and the check runs BEFORE the (slow) Argon2 hash, so an attacker cannot
  make us spend hashing time. The cost: someone can lock a real user out of their email for
  one window by typing it wrongly on purpose.
- run starts and customer replies, per customer per hour (model cost abuse).

Clients are stored as an HMAC-SHA256 of the value, keyed with JWT_SECRET: the table holds no
email, IP address or customer id, and a list of known emails cannot be checked against it
without the key. Nothing here logs the value.

The counter is committed at once (`session.commit()`), because the request may fail
afterwards (a 401, a 404) and the attempt must still count.
"""

import hashlib
import hmac
import logging
import math
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.repositories import rate_limits

log = logging.getLogger(__name__)

_HOUR_SECONDS = 3600


class Scope(StrEnum):
    LOGIN_EMAIL = "login_email"
    LOGIN_IP = "login_ip"
    RUN_START = "run_start"
    CUSTOMER_REPLY = "customer_reply"


def _bucket_key(settings: Settings, scope: Scope, identity: str) -> str:
    secret = settings.jwt_secret.get_secret_value().encode()
    digest = hmac.new(secret, f"{scope.value}|{identity}".encode(), hashlib.sha256).hexdigest()
    return f"{scope.value}:{digest}"


def _window(now: datetime, window_seconds: int) -> tuple[datetime, datetime]:
    """The fixed window `now` falls in: (start, end)."""
    epoch = int(now.timestamp())
    start = epoch - epoch % window_seconds
    return (
        datetime.fromtimestamp(start, UTC),
        datetime.fromtimestamp(start + window_seconds, UTC),
    )


def too_many_requests(scope: Scope, limit: int, window_seconds: int, retry_after: int) -> AppError:
    return AppError(
        429,
        "RATE_LIMITED",
        "Too many requests. Please wait before trying again.",
        headers={"Retry-After": str(retry_after)},
        details={
            "scope": scope.value,
            "limit": limit,
            "window_seconds": window_seconds,
            "retry_after_seconds": retry_after,
        },
    )


async def hit(
    session: AsyncSession,
    settings: Settings,
    *,
    scope: Scope,
    identity: str,
    limit: int,
    window_seconds: int,
    now: datetime,
) -> None:
    """Count one request for `identity`; raise 429 if it is over `limit` in this window."""
    if not settings.rate_limit_enabled:
        return
    start, end = _window(now, window_seconds)
    await rate_limits.delete_expired(session, now)
    count = await rate_limits.increment(
        session,
        bucket_key=_bucket_key(settings, scope, identity),
        window_start=start,
        expires_at=end,
    )
    await session.commit()  # the attempt counts even if the request fails afterwards
    if count > limit:
        retry_after = max(1, math.ceil((end - now).total_seconds()))
        log.warning("rate_limited", extra={"scope": scope.value})
        raise too_many_requests(scope, limit, window_seconds, retry_after)


async def check_login(
    session: AsyncSession, settings: Settings, *, email: str, client_ip: str, now: datetime
) -> None:
    """Brute-force guard for POST /auth/login: per client IP first, then per email."""
    window = settings.rate_limit_login_window_seconds
    await hit(
        session,
        settings,
        scope=Scope.LOGIN_IP,
        identity=client_ip,
        limit=settings.rate_limit_login_per_ip,
        window_seconds=window,
        now=now,
    )
    await hit(
        session,
        settings,
        scope=Scope.LOGIN_EMAIL,
        identity=email.strip().lower(),
        limit=settings.rate_limit_login_per_email,
        window_seconds=window,
        now=now,
    )


async def check_run_start(
    session: AsyncSession, settings: Settings, *, customer_id: str, now: datetime
) -> None:
    await hit(
        session,
        settings,
        scope=Scope.RUN_START,
        identity=customer_id,
        limit=settings.rate_limit_run_starts_per_hour,
        window_seconds=_HOUR_SECONDS,
        now=now,
    )


async def check_customer_reply(
    session: AsyncSession, settings: Settings, *, customer_id: str, now: datetime
) -> None:
    await hit(
        session,
        settings,
        scope=Scope.CUSTOMER_REPLY,
        identity=customer_id,
        limit=settings.rate_limit_replies_per_hour,
        window_seconds=_HOUR_SECONDS,
        now=now,
    )
