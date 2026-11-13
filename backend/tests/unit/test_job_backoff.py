"""Retry backoff and error codes for the job queue (worker/queue.py). Pure functions."""

import pytest

from support_agent.core.errors import AppError
from support_agent.worker.queue import backoff_delay, error_code


def delay(attempt: int, rand: float) -> float:
    return backoff_delay(attempt, base_seconds=2.0, max_seconds=60.0, rand=lambda: rand)


def test_the_ceiling_doubles_with_each_attempt() -> None:
    # rand = 1.0 gives the ceiling itself: 2, 4, 8, 16 seconds.
    assert [delay(n, 1.0) for n in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 16.0]


def test_the_wait_is_capped() -> None:
    assert delay(20, 1.0) == 60.0


@pytest.mark.parametrize("attempt", [1, 3, 10])
def test_jitter_keeps_the_wait_in_the_top_half_of_the_ceiling(attempt: int) -> None:
    ceiling = delay(attempt, 1.0)
    assert delay(attempt, 0.0) == ceiling / 2
    assert ceiling / 2 <= delay(attempt, 0.37) <= ceiling


def test_error_codes_never_carry_the_message() -> None:
    assert error_code(AppError(409, "AMOUNT_CHANGED", "customer text here")) == "AMOUNT_CHANGED"
    assert error_code(ConnectionError("customer text here")) == "ConnectionError"
