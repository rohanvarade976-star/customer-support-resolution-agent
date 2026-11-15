"""Counting one run segment's LLM calls, tokens and time.

`metered()` starts a meter for the current task. `RoutedLLMClient` reports every successful
provider answer to it (`record_usage`). The workflow's node wrapper and the critic report how
long they took (`timed`). The meter lives in a context variable, so agents need no extra
argument, and the parallel branches of a workflow (which copy the context but share the same
meter object) all add to the same counts.

Only names and numbers are kept: never prompts or answers.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from support_agent.llm.types import Usage


@dataclass
class UsageMeter:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0  # includes reasoning tokens, as in `Usage`
    fallback_calls: int = 0  # answers that came from a provider other than the first
    # Milliseconds spent per step name ("classify", "investigate", "critic", ...), added up
    # when a step runs more than once in a segment.
    durations_ms: dict[str, int] = field(default_factory=dict)

    def add(self, usage: Usage, *, fallback: bool = False) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        if fallback:
            self.fallback_calls += 1

    def add_duration(self, name: str, ms: int) -> None:
        self.durations_ms[name] = self.durations_ms.get(name, 0) + ms


# The step name for a whole segment's time (the worker's active time on the run).
SEGMENT = "segment"

_current: ContextVar[UsageMeter | None] = ContextVar("llm_usage_meter", default=None)


@contextmanager
def metered() -> Iterator[UsageMeter]:
    meter = UsageMeter()
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)


def record_usage(usage: Usage, *, fallback: bool = False) -> None:
    """Add one successful call to the current meter (nothing happens if none is running)."""
    meter = _current.get()
    if meter is not None:
        meter.add(usage, fallback=fallback)


def elapsed_ms(started: float) -> int:
    """Whole milliseconds since a `time.perf_counter()` reading."""
    return round((time.perf_counter() - started) * 1000)


@contextmanager
def timed(name: str) -> Iterator[None]:
    """Add the time spent inside the block to the current meter under `name`.

    The time is recorded even if the block raises (a pause is an exception in LangGraph).
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        meter = _current.get()
        if meter is not None:
            meter.add_duration(name, elapsed_ms(started))
