"""In-memory failure switch for the mock payment provider.

An admin sets a fault mode for the next N requests; each /refunds request then takes
one fault (and uses up one of the N). Restarting the mock resets it to "none".
"""

import asyncio

from mock_providers.payments.schemas import FaultConfig, FaultMode, FaultState


class FaultInjector:
    def __init__(self) -> None:
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

        "decline" only makes sense when creating a refund, so reads do not use it up.
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
