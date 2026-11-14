"""Client-side pacing for eval runs, so a long run stays inside the free-tier limits.

Groq's free tier limits requests per minute (RPM) and tokens per minute (TPM). Hitting them
makes the provider answer 429, the client backs off, and after a few tries the run escalates
with LLM_UNAVAILABLE: a wasted case. So the eval paces itself instead.

A token bucket holds up to `capacity` tokens and refills at `rate` per second. Taking from an
empty bucket waits until enough has refilled. Two buckets per provider:

- requests: each provider attempt (retries included) takes 1 before it is sent;
- tokens: the call's real token count is only known afterwards, so it is taken then, and
  may push the bucket below zero. The next call waits until the bucket is back to zero.

Each provider (Groq, Gemini) gets its own limiter, because each has its own quota. The
limiter sits below `RoutedLLMClient`, so retries and the fallback still work as in the app.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from support_agent.llm.client import ChatProvider
from support_agent.llm.types import LLMRequest, ModelTier, ProviderReply

Clock = Callable[[], float]  # seconds, monotonic
Sleep = Callable[[float], Awaitable[None]]


class TokenBucket:
    def __init__(
        self,
        capacity: float,
        per_minute: float,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if capacity <= 0 or per_minute <= 0:
            raise ValueError("capacity and per_minute must be positive")
        self.capacity = capacity
        self.rate = per_minute / 60.0  # tokens per second
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity  # start full
        self._updated = clock()
        self._lock = asyncio.Lock()  # parallel branches share one bucket

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self, amount: float = 1.0) -> float:
        """Take `amount`, waiting until the bucket holds it. Returns the seconds waited.

        `amount=0` waits only while the bucket is below zero (after a large `debit`).
        """
        waited = 0.0
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= amount:
                    self._tokens -= amount
                    return waited
                delay = (amount - self._tokens) / self.rate
                await self._sleep(delay)
                waited += delay

    def debit(self, amount: float) -> None:
        """Take `amount` now, without waiting; the bucket may go negative."""
        self._refill()
        self._tokens -= amount


@dataclass
class RateLimiter:
    requests: TokenBucket
    tokens: TokenBucket | None = None
    waited_seconds: float = 0.0  # running total, so the harness can subtract it from latency

    async def before_call(self) -> None:
        waited = await self.requests.acquire(1)
        if self.tokens is not None:
            waited += await self.tokens.acquire(0)
        self.waited_seconds += waited

    def after_call(self, total_tokens: int) -> None:
        if self.tokens is not None:
            self.tokens.debit(total_tokens)


def build_limiter(rpm: float, tpm: float | None) -> RateLimiter:
    # Capacity: a few requests may go out back to back (the parallel branches), then steady
    # pacing. Tokens: at most one minute's worth in reserve.
    requests = TokenBucket(capacity=min(rpm, 3.0), per_minute=rpm)
    tokens = None if tpm is None else TokenBucket(capacity=tpm, per_minute=tpm)
    return RateLimiter(requests, tokens)


class RateLimitedProvider:
    """A ChatProvider that waits for its limiter before every call."""

    def __init__(self, inner: ChatProvider, limiter: RateLimiter) -> None:
        self._inner = inner
        self.limiter = limiter

    @property
    def name(self) -> str:
        return self._inner.name

    def model_for(self, tier: ModelTier) -> str:
        return self._inner.model_for(tier)

    async def generate(self, request: LLMRequest) -> ProviderReply:
        await self.limiter.before_call()
        reply = await self._inner.generate(request)
        self.limiter.after_call(reply.usage.input_tokens + reply.usage.output_tokens)
        return reply
