"""Counting one run's LLM calls and tokens.

`metered()` starts a meter for the current task. `RoutedLLMClient` reports every successful
provider answer to it (`record_usage`). The meter lives in a context variable, so agents
need no extra argument, and the parallel branches of a workflow (which copy the context but
share the same meter object) all add to the same counts.

Only numbers are kept: never prompts or answers.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from support_agent.llm.types import Usage


@dataclass
class UsageMeter:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0  # includes reasoning tokens, as in `Usage`

    def add(self, usage: Usage) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens


_current: ContextVar[UsageMeter | None] = ContextVar("llm_usage_meter", default=None)


@contextmanager
def metered() -> Iterator[UsageMeter]:
    meter = UsageMeter()
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)


def record_usage(usage: Usage) -> None:
    """Add one successful call to the current meter (nothing happens if none is running)."""
    meter = _current.get()
    if meter is not None:
        meter.add(usage)
