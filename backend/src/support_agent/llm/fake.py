"""A deterministic, scripted LLM provider for tests. No network, no cost.

You give it a script: a list of steps. Each call to `generate` uses the next step:
- a `ProviderReply` is returned (build one with `text_reply` or `tool_reply`),
- an exception is raised (use `rate_limited()`, `transient()`, `invalid_json()`, or any
  LLMError).

It plugs in below RoutedLLMClient, so tests exercise the real retry, fallback and
repair code. Every request it receives is kept in `requests` for assertions.

Example:
    fake = FakeProvider([rate_limited(), text_reply("Hello")])
"""

from typing import Any

from support_agent.llm.errors import (
    LLMError,
    LLMInvalidOutputError,
    LLMRateLimitError,
    LLMTransientError,
)
from support_agent.llm.types import LLMRequest, ModelTier, ProviderReply, ToolCall, Usage

ScriptStep = ProviderReply | LLMError


def text_reply(content: str, *, input_tokens: int = 10, output_tokens: int = 5) -> ProviderReply:
    return ProviderReply(
        content=content,
        tool_calls=[],
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def tool_reply(name: str, args: dict[str, Any], *, call_id: str = "call_0") -> ProviderReply:
    return ProviderReply(
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, args=args)],
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def rate_limited() -> LLMRateLimitError:
    return LLMRateLimitError("fake rate limit", retry_after_seconds=None)


def transient() -> LLMTransientError:
    return LLMTransientError("fake transient failure")


def invalid_json() -> LLMInvalidOutputError:
    """What real providers raise when tool-call arguments are not valid JSON."""
    return LLMInvalidOutputError("fake invalid tool call JSON")


class FakeProvider:
    def __init__(self, script: list[ScriptStep], *, name: str = "fake") -> None:
        self._script = list(script)
        self._name = name
        self.requests: list[LLMRequest] = []

    @property
    def name(self) -> str:
        return self._name

    def model_for(self, tier: ModelTier) -> str:
        return f"{self._name}-{tier}"

    async def generate(self, request: LLMRequest) -> ProviderReply:
        self.requests.append(request)
        if not self._script:
            raise LLMError(
                "FakeProvider script is exhausted: the test made more calls than scripted."
            )
        step = self._script.pop(0)
        if isinstance(step, LLMError):
            raise step
        return step
