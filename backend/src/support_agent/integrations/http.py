"""Shared HTTP machinery used by every provider client.

`ProviderHttp` wraps one httpx client per provider. It:
- applies the provider's base URL and timeouts,
- turns network failures and error responses into our errors (errors.py),
- retries only what is safe to retry, with exponential backoff and jitter,
- logs one structured line per attempt (never headers, bodies or query strings).

Retried: timeouts, connection failures and 5xx responses (ProviderUnavailableError).
Never retried: 4xx responses, because the same request gets the same answer.

Each client chooses per call: `request()` retries, `send_once()` does not.
Refund creation uses `send_once()` only (see docs/decisions.md, ADR-004).
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Self

import httpx
from pydantic import BaseModel, ValidationError

from support_agent.core.config import Settings
from support_agent.integrations.errors import (
    IntegrationError,
    ProviderRejectedError,
    ProviderUnavailableError,
    ResourceNotFoundError,
)

log = logging.getLogger(__name__)

# Failures that happen before the request leaves our machine.
# The provider cannot have acted on a request it never received.
_NOT_SENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
)


# ---------------------------------------------------------------- retry


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_base_seconds: float


def backoff_delay(attempt: int, base_seconds: float) -> float:
    """Seconds to wait after failed attempt number `attempt` (counting from 1).

    "Full jitter": a random wait between 0 and base * 2^(attempt - 1). The randomness
    stops many clients from retrying against a recovering provider at the same moment.
    """
    ceiling = base_seconds * 2 ** (attempt - 1)
    return random.uniform(0, ceiling)  # noqa: S311  # jitter only, not security


async def retry_async[T](operation: Callable[[int], Awaitable[T]], policy: RetryPolicy) -> T:
    """Run `operation(attempt)` until it succeeds or the attempts run out.

    Only ProviderUnavailableError is retried. Any other error is raised immediately.
    """
    attempt = 1
    while True:
        try:
            return await operation(attempt)
        except ProviderUnavailableError:
            if attempt >= policy.max_attempts:
                raise
        await asyncio.sleep(backoff_delay(attempt, policy.backoff_base_seconds))
        attempt += 1


# ---------------------------------------------------------------- error mapping


class _ErrorBody(BaseModel):
    """The structured error body every provider returns (extra fields are ignored)."""

    error_code: str
    message: str


def error_from_response(provider: str, response: httpx.Response) -> IntegrationError:
    """Turn a non-2xx response into one of our errors.

    The choice depends on the status code only. The provider's own "retryable" flag is
    not trusted: our retry rules must not change because a provider says so.
    """
    status = response.status_code
    try:
        body = _ErrorBody.model_validate_json(response.content)
        error_code, message = body.error_code, body.message
    except ValidationError:  # not the structured format, e.g. a proxy's HTML error page
        error_code, message = f"HTTP_{status}", f"Provider returned HTTP {status}."

    if status >= 500:
        return ProviderUnavailableError(provider, error_code, message, request_sent=True)
    if status == 404:
        return ResourceNotFoundError(provider, error_code, message)
    return ProviderRejectedError(provider, status, error_code, message)


# ---------------------------------------------------------------- HTTP client


class ProviderHttp:
    def __init__(
        self,
        provider: str,
        base_url: str,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.retry_policy = RetryPolicy(
            max_attempts=settings.provider_max_attempts,
            backoff_base_seconds=settings.provider_backoff_base_seconds,
        )
        connect = settings.provider_connect_timeout_seconds
        read = settings.provider_read_timeout_seconds
        # httpx enforces these on real network connections.
        timeout = httpx.Timeout(connect=connect, read=read, write=read, pool=connect)
        # Plus one overall deadline per attempt. It also works for in-memory transports
        # (httpx.ASGITransport ignores httpx timeouts), which the tests rely on.
        self._attempt_deadline = connect + read
        # transport=None means "real network"; tests pass an in-memory transport.
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send with retries. Use only for reads and idempotent writes."""

        async def attempt_once(attempt: int) -> httpx.Response:
            return await self.send_once(
                method, path, json=json, params=params, headers=headers, attempt=attempt
            )

        return await retry_async(attempt_once, self.retry_policy)

    async def send_once(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        attempt: int = 1,
    ) -> httpx.Response:
        """One attempt, no retry. Returns a 2xx response or raises an IntegrationError."""
        started = time.perf_counter()
        try:
            response = await self._send(method, path, json=json, params=params, headers=headers)
        except IntegrationError as network_error:
            self._log_attempt(method, path, attempt, started, status=None, error=network_error)
            raise

        if response.is_success:
            self._log_attempt(method, path, attempt, started, status=response.status_code)
            return response
        error = error_from_response(self.provider, response)
        self._log_attempt(method, path, attempt, started, status=response.status_code, error=error)
        raise error

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        """Send the request, turning network failures into ProviderUnavailableError."""
        try:
            async with asyncio.timeout(self._attempt_deadline):
                return await self._client.request(
                    method, path, json=json, params=params, headers=headers
                )
        # Order matters: ConnectTimeout is also a TimeoutException, and it means "not sent".
        except _NOT_SENT_ERRORS as exc:
            raise ProviderUnavailableError(
                self.provider,
                "CONNECTION_FAILED",
                "Could not connect to the provider.",
                request_sent=False,
            ) from exc
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ProviderUnavailableError(
                self.provider,
                "TIMEOUT",
                "The provider did not respond in time.",
                request_sent=True,
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(
                self.provider,
                "CONNECTION_LOST",
                "The connection failed after the request was sent.",
                request_sent=True,
            ) from exc

    def _log_attempt(
        self,
        method: str,
        path: str,
        attempt: int,
        started: float,
        *,
        status: int | None,
        error: IntegrationError | None = None,
    ) -> None:
        # Path only: query strings (which can hold idempotency keys), headers and bodies
        # are never logged.
        fields = {
            "provider": self.provider,
            "method": method,
            "path": path,
            "status": status,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "attempt": attempt,
            "outcome": "ok" if error is None else type(error).__name__,
            "error_code": None if error is None else error.error_code,
        }
        if error is None:
            log.info("provider call succeeded", extra=fields)
        else:
            log.warning("provider call failed", extra=fields)


class BaseProviderClient:
    """Shared lifecycle for the provider clients: use with `async with`, or call aclose()."""

    def __init__(self, http: ProviderHttp) -> None:
        self._http = http

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
