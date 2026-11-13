"""The one doorway every agent uses to talk to an LLM.

`LLMClient` is the interface agents depend on. `RoutedLLMClient` implements it:

    for each provider, in order (Groq first, then Gemini if configured):
        try up to `max_attempts` times, waiting with backoff after a rate limit
        or transient error
        log one usage line per attempt
    if every provider ran out of attempts: raise LLMUnavailableError

Errors that retrying cannot fix (bad request, bad key, invalid output) are raised at
once, with no retry and no fallback.

The usage log line holds provider, model, tier, attempt, outcome, token counts and
latency. It never holds prompt text, response text or keys.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from support_agent.llm import structured
from support_agent.llm.errors import (
    LLMError,
    LLMRateLimitError,
    LLMTransientError,
    LLMUnavailableError,
)
from support_agent.llm.types import (
    ChatMessage,
    LLMRequest,
    LLMResult,
    ModelTier,
    ProviderReply,
    ToolSpec,
    check_message_order,
)

log = logging.getLogger(__name__)

# Errors worth retrying and falling back on: the same request may succeed later.
RETRYABLE_ERRORS = (LLMRateLimitError, LLMTransientError)


class ChatProvider(Protocol):
    """One LLM provider (Groq, Gemini or the fake). Implemented in providers.py / fake.py."""

    @property
    def name(self) -> str: ...

    def model_for(self, tier: ModelTier) -> str: ...

    async def generate(self, request: LLMRequest) -> ProviderReply: ...


class LLMClient(Protocol):
    """What agents use. They never see providers, retries or LangChain."""

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tier: ModelTier = "small",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult: ...

    async def structured[S: BaseModel](
        self,
        messages: list[ChatMessage],
        schema: type[S],
        *,
        tier: ModelTier = "small",
    ) -> S: ...


@dataclass(frozen=True)
class RetrySettings:
    max_attempts: int  # per provider, including the first try
    backoff_base_seconds: float
    backoff_max_seconds: float


def backoff_delay(
    attempt: int, settings: RetrySettings, retry_after_seconds: float | None = None
) -> float:
    """Seconds to wait after failed attempt number `attempt` (counting from 1).

    Full jitter: a random wait between 0 and base * 2^(attempt - 1). If the provider
    told us how long to wait (Retry-After), we wait at least that long. Both are capped
    at `backoff_max_seconds` so one call can never hang for minutes.
    """
    ceiling = settings.backoff_base_seconds * 2 ** (attempt - 1)
    delay = random.uniform(0, ceiling)  # noqa: S311  # jitter only, not security
    if retry_after_seconds is not None:
        delay = max(delay, retry_after_seconds)
    return min(delay, settings.backoff_max_seconds)


class RoutedLLMClient:
    """LLMClient with retries, provider fallback and usage logging."""

    def __init__(
        self,
        providers: list[ChatProvider],
        retry: RetrySettings,
        *,
        default_temperature: float,
        default_max_tokens: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,  # tests skip waiting
    ) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers = providers
        self._retry = retry
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._sleep = sleep

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tier: ModelTier = "small",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        return await self.complete(
            LLMRequest(
                messages=messages,
                tier=tier,
                temperature=self._default_temperature if temperature is None else temperature,
                max_tokens=max_tokens or self._default_max_tokens,
                tools=tools or [],
            )
        )

    async def structured[S: BaseModel](
        self,
        messages: list[ChatMessage],
        schema: type[S],
        *,
        tier: ModelTier = "small",
    ) -> S:
        return await structured.generate_structured(self, messages, schema, tier=tier)

    def request_defaults(self) -> tuple[float, int]:
        """(temperature, max_tokens) used when a caller does not choose."""
        return self._default_temperature, self._default_max_tokens

    async def complete(self, request: LLMRequest) -> LLMResult:
        """Run one request through the providers in order. Used by chat and structured."""
        check_message_order(request.messages)
        last_error: LLMError | None = None
        for provider in self._providers:
            try:
                return await self._call_with_retries(provider, request)
            except RETRYABLE_ERRORS as exc:
                log.warning(
                    "llm_provider_failed",
                    extra={"provider": provider.name, "error_type": type(exc).__name__},
                )
                last_error = exc
        raise LLMUnavailableError(
            "No LLM provider is available right now (rate limited or failing). Try again later."
        ) from last_error

    async def _call_with_retries(self, provider: ChatProvider, request: LLMRequest) -> LLMResult:
        model = provider.model_for(request.tier)
        attempt = 1
        while True:
            started = time.perf_counter()
            try:
                reply = await provider.generate(request)
            except Exception as exc:
                _log_usage(provider.name, model, request.tier, attempt, started, error=exc)
                if not isinstance(exc, RETRYABLE_ERRORS) or attempt >= self._retry.max_attempts:
                    raise
                retry_after = (
                    exc.retry_after_seconds if isinstance(exc, LLMRateLimitError) else None
                )
                await self._sleep(backoff_delay(attempt, self._retry, retry_after))
                attempt += 1
                continue
            _log_usage(provider.name, model, request.tier, attempt, started, reply=reply)
            return LLMResult(
                content=reply.content,
                tool_calls=reply.tool_calls,
                provider=provider.name,
                model=model,
                usage=reply.usage,
            )


def _log_usage(
    provider: str,
    model: str,
    tier: ModelTier,
    attempt: int,
    started: float,
    *,
    reply: ProviderReply | None = None,
    error: Exception | None = None,
) -> None:
    """One line per attempt. Only numbers and names: no prompt, response or key."""
    fields: dict[str, object] = {
        "provider": provider,
        "model": model,
        "tier": tier,
        "attempt": attempt,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }
    if reply is not None:
        fields["outcome"] = "ok"
        fields["input_tokens"] = reply.usage.input_tokens
        fields["output_tokens"] = reply.usage.output_tokens
        fields["reasoning_tokens"] = reply.usage.reasoning_tokens
        log.info("llm_call", extra=fields)
    else:
        fields["outcome"] = "error"
        # The class name only: SDK error messages can echo generated content.
        fields["error_type"] = type(error).__name__
        log.warning("llm_call", extra=fields)
