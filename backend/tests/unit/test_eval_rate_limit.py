"""The eval's client-side rate limiter (evals/rate_limit.py), on a fake clock: no real waiting."""

import pytest

from evals.rate_limit import RateLimitedProvider, RateLimiter, TokenBucket
from support_agent.llm.fake import FakeProvider, text_reply
from support_agent.llm.types import ChatMessage, LLMRequest


class FakeTime:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def bucket(t: FakeTime, capacity: float, per_minute: float) -> TokenBucket:
    return TokenBucket(capacity, per_minute, clock=t.clock, sleep=t.sleep)


async def test_a_full_bucket_lets_a_burst_through_without_waiting() -> None:
    t = FakeTime()
    b = bucket(t, capacity=3, per_minute=60)

    waits = [await b.acquire() for _ in range(3)]

    assert waits == [0, 0, 0]
    assert t.sleeps == []


async def test_an_empty_bucket_waits_for_the_refill() -> None:
    t = FakeTime()
    b = bucket(t, capacity=1, per_minute=30)  # one token every 2 seconds
    await b.acquire()

    waited = await b.acquire()

    assert waited == pytest.approx(2.0)
    assert t.now == pytest.approx(2.0)


async def test_the_bucket_never_refills_beyond_its_capacity() -> None:
    t = FakeTime()
    b = bucket(t, capacity=2, per_minute=60)
    t.now = 1000  # a long idle time

    assert b.tokens == 2


async def test_a_debit_below_zero_makes_the_next_call_wait() -> None:
    t = FakeTime()
    b = bucket(t, capacity=600, per_minute=600)  # 10 tokens per second
    b.debit(700)  # a big call: 100 below zero

    waited = await b.acquire(0)

    assert waited == pytest.approx(10.0)


def test_invalid_limits_are_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(0, 60)


async def test_the_provider_waits_before_each_call_and_debits_the_real_tokens() -> None:
    t = FakeTime()
    limiter = RateLimiter(
        requests=bucket(t, capacity=1, per_minute=60),
        tokens=bucket(t, capacity=100, per_minute=60),
    )
    inner = FakeProvider(
        [
            text_reply("one", input_tokens=150, output_tokens=10),
            text_reply("two", input_tokens=1, output_tokens=1),
        ]
    )
    provider = RateLimitedProvider(inner, limiter)
    request = LLMRequest(
        messages=[ChatMessage(role="user", content="hi")], tier="small", temperature=0, max_tokens=8
    )

    await provider.generate(request)  # no wait; its 160 tokens take the bucket to -60
    # Then: 1s for a request token, and the remaining 59s for the token bucket to reach 0.
    await provider.generate(request)

    assert t.sleeps == pytest.approx([1.0, 59.0])
    assert limiter.waited_seconds == pytest.approx(60.0)
    assert provider.name == inner.name
