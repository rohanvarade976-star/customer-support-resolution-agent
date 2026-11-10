"""In-memory failure switch shared by all mock providers.

An admin sets a fault mode for the next N requests; each matching request then takes
one fault (and uses up one of the N). Restarting a mock resets it to "none".
"""

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from mock_providers.common.errors import error_response


class FaultMode(StrEnum):
    NONE = "none"
    SLOW = "slow"  # wait delay_ms, then handle normally
    ERROR = "error"  # 500 without running the route (nothing is saved)
    DROP_RESPONSE = "drop_response"  # do the work, then reply 504 as if the reply was lost
    DECLINE = "decline"  # payments only: newly created refunds end as "failed"


# Modes every provider supports. Payments adds DECLINE.
BASIC_MODES = frozenset({FaultMode.NONE, FaultMode.SLOW, FaultMode.ERROR, FaultMode.DROP_RESPONSE})


class FaultConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: FaultMode
    # How many upcoming requests the fault applies to.
    count: StrictInt = Field(default=1, ge=0)
    # Only used by "slow".
    delay_ms: StrictInt = Field(default=0, ge=0, le=60_000)

    @model_validator(mode="after")
    def _slow_needs_delay(self) -> "FaultConfig":
        if self.mode is FaultMode.SLOW and self.delay_ms == 0:
            raise ValueError("mode 'slow' needs delay_ms > 0")
        return self


class FaultState(BaseModel):
    mode: FaultMode
    remaining: int
    delay_ms: int


class FaultInjector:
    def __init__(self, supported_modes: frozenset[FaultMode] = BASIC_MODES) -> None:
        self.supported_modes = supported_modes
        self._mode = FaultMode.NONE
        self._remaining = 0
        self._delay_ms = 0
        # Two concurrent requests must not both take the last remaining fault.
        self._lock = asyncio.Lock()

    async def configure(self, config: FaultConfig) -> FaultState:
        async with self._lock:
            self._mode = config.mode
            self._remaining = 0 if config.mode is FaultMode.NONE else config.count
            self._delay_ms = config.delay_ms
            return self._state()

    async def state(self) -> FaultState:
        async with self._lock:
            return self._state()

    async def take(self, *, is_create: bool) -> tuple[FaultMode, int]:
        """Return (mode, delay_ms) for one request, using up one of the remaining count.

        "decline" only makes sense when creating something, so reads do not use it up.
        """
        async with self._lock:
            if self._remaining <= 0:
                return FaultMode.NONE, 0
            if self._mode is FaultMode.DECLINE and not is_create:
                return FaultMode.NONE, 0
            self._remaining -= 1
            return self._mode, self._delay_ms

    def _state(self) -> FaultState:
        if self._remaining <= 0:
            return FaultState(mode=FaultMode.NONE, remaining=0, delay_ms=0)
        return FaultState(mode=self._mode, remaining=self._remaining, delay_ms=self._delay_ms)


def install_fault_middleware(app: FastAPI, faults: FaultInjector, path_prefix: str) -> None:
    """Apply faults to requests under `path_prefix` (e.g. "/refunds"). Admin routes never fault.

    The chosen mode is stored on `request.state.fault_mode` so a route can react to it
    (payments uses this for "decline").
    """

    def is_faultable(path: str) -> bool:
        return path == path_prefix or path.startswith(path_prefix + "/")

    @app.middleware("http")
    async def inject_faults(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not is_faultable(request.url.path):
            return await call_next(request)

        mode, delay_ms = await faults.take(is_create=request.method == "POST")
        if mode is FaultMode.SLOW:
            await asyncio.sleep(delay_ms / 1000)
        elif mode is FaultMode.ERROR:
            return error_response(500, "PROVIDER_ERROR", "Internal provider error.", True)

        request.state.fault_mode = mode
        response = await call_next(request)  # the route runs fully, including any DB write

        if mode is FaultMode.DROP_RESPONSE:
            # The work is done, but the caller never sees the real reply.
            return error_response(504, "GATEWAY_TIMEOUT", "Provider did not respond in time.", True)
        return response
