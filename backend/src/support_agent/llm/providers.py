"""Real LLM providers (Groq, Gemini) and the factory that picks providers from config.

Each adapter:
- converts our ChatMessage / ToolSpec types to LangChain's and back,
- uses LangChain's model layer only (ChatGroq / ChatGoogleGenerativeAI, `bind_tools`),
- turns off LangChain's own retries, so the client's retry loop is the only one,
- maps every SDK error to an error from llm/errors.py, without copying SDK messages
  (they can contain generated text).
"""

from abc import ABC, abstractmethod
from typing import Any, Literal

import groq
import httpx
from langchain_core.exceptions import ModelError, ModelRateLimitError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
from pydantic import SecretStr

from support_agent.core.config import Settings
from support_agent.llm.client import ChatProvider, RetrySettings, RoutedLLMClient
from support_agent.llm.errors import (
    LLMConfigError,
    LLMInvalidOutputError,
    LLMRateLimitError,
    LLMRequestError,
    LLMTransientError,
)
from support_agent.llm.fake import FakeProvider
from support_agent.llm.types import (
    ChatMessage,
    LLMRequest,
    ModelTier,
    ProviderReply,
    ToolCall,
    Usage,
)

# ---------------------------------------------------------------- conversion


def to_langchain(messages: list[ChatMessage]) -> list[BaseMessage]:
    converted: list[BaseMessage] = []
    for message in messages:
        match message.role:
            case "system":
                converted.append(SystemMessage(content=message.content))
            case "user":
                converted.append(HumanMessage(content=message.content))
            case "assistant":
                calls = [
                    {"id": c.id, "name": c.name, "args": c.args, "type": "tool_call"}
                    for c in message.tool_calls
                ]
                converted.append(AIMessage(content=message.content, tool_calls=calls))
            case "tool":
                assert message.tool_call_id is not None  # guaranteed by ChatMessage  # noqa: S101
                converted.append(
                    ToolMessage(content=message.content, tool_call_id=message.tool_call_id)
                )
    return converted


def from_langchain(message: BaseMessage) -> ProviderReply:
    if not isinstance(message, AIMessage):
        raise LLMRequestError(f"Expected an AI message, got {type(message).__name__}")
    if message.invalid_tool_calls:
        # The model tried to call a tool but the arguments were not valid JSON.
        raise LLMInvalidOutputError("The model returned a tool call with invalid arguments.")
    calls = [
        ToolCall(id=call.get("id") or f"call_{index}", name=call["name"], args=call["args"])
        for index, call in enumerate(message.tool_calls)
    ]
    usage = Usage()  # zeros if the provider did not report token counts
    if message.usage_metadata is not None:
        details = message.usage_metadata.get("output_token_details", {})
        usage = Usage(
            input_tokens=message.usage_metadata["input_tokens"],
            output_tokens=message.usage_metadata["output_tokens"],
            reasoning_tokens=details.get("reasoning", 0),
        )
    return ProviderReply(content=message.text, tool_calls=calls, usage=usage)


def _tool_dicts(request: LLMRequest) -> list[dict[str, Any]]:
    """Tools in the OpenAI function format, which both LangChain adapters accept."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in request.tools
    ]


class _LangChainProvider(ABC):
    """Shared plumbing: model choice, model caching, tool binding and the call itself."""

    name: str

    def __init__(self, small_model: str, large_model: str) -> None:
        self._models: dict[ModelTier, str] = {"small": small_model, "large": large_model}
        # One LangChain model object per (model, temperature, max_tokens), reused across
        # calls so each call does not open a new HTTP connection pool.
        self._cache: dict[tuple[str, float, int], BaseChatModel] = {}

    def model_for(self, tier: ModelTier) -> str:
        return self._models[tier]

    @abstractmethod
    def _build(self, model: str, temperature: float, max_tokens: int) -> BaseChatModel:
        """Create the LangChain chat model for these settings."""

    async def _invoke(self, request: LLMRequest) -> ProviderReply:
        model = self.model_for(request.tier)
        key = (model, request.temperature, request.max_tokens)
        if key not in self._cache:
            self._cache[key] = self._build(*key)
        chat_model = self._cache[key]

        runnable: Any = chat_model
        if request.tools:
            runnable = chat_model.bind_tools(_tool_dicts(request), tool_choice=request.forced_tool)
        reply = await runnable.ainvoke(to_langchain(request.messages))
        return from_langchain(reply)


# ---------------------------------------------------------------- Groq


class GroqProvider(_LangChainProvider):
    name = "groq"

    def __init__(
        self,
        api_key: SecretStr,
        small_model: str,
        large_model: str,
        timeout_seconds: float,
        reasoning_effort: Literal["low", "medium", "high"] | None = None,
    ) -> None:
        super().__init__(small_model, large_model)
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._reasoning_effort = reasoning_effort  # None: not sent (non-reasoning models)

    def _build(self, model: str, temperature: float, max_tokens: int) -> BaseChatModel:
        return ChatGroq(
            # Field names, not the aliases (model=, api_key=, timeout=), so mypy can check them.
            model_name=model,
            groq_api_key=self._api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=self._reasoning_effort,
            request_timeout=self._timeout,
            max_retries=0,  # our client retries; never two retry loops
        )

    async def generate(self, request: LLMRequest) -> ProviderReply:
        # `from None` on purpose: SDK errors carry the response body, which can include
        # the model's output ("failed_generation") and so customer text. Attaching them
        # would put that text into any traceback that gets logged.
        try:
            return await self._invoke(request)
        except groq.RateLimitError as exc:
            raise LLMRateLimitError(
                "Groq rate limit reached (HTTP 429).",
                retry_after_seconds=_retry_after(exc.response),
            ) from None
        except groq.APIConnectionError as exc:  # includes APITimeoutError
            raise LLMTransientError(f"Groq connection failed ({type(exc).__name__}).") from None
        except groq.APIStatusError as exc:
            code = _groq_error_code(exc.body)
            if exc.status_code >= 500:
                raise LLMTransientError(f"Groq server error (HTTP {exc.status_code}).") from None
            if code == "tool_use_failed":
                # Groq checks tool calls itself and rejects malformed ones with a 400.
                raise LLMInvalidOutputError(
                    "Groq: the model produced an invalid tool call."
                ) from None
            raise LLMRequestError(
                f"Groq rejected the request (HTTP {exc.status_code}, code {code})."
            ) from None


def _groq_error_code(body: object) -> str:
    """Groq's short machine-readable error code, e.g. "model_not_found". Safe to log."""
    if isinstance(body, dict):
        error = body.get("error", body)  # the SDK sometimes unwraps {"error": {...}}
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return str(error["code"])
    return "unknown"


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # an HTTP date instead of seconds; fall back to our own backoff


# ---------------------------------------------------------------- Gemini


class GeminiProvider(_LangChainProvider):
    name = "gemini"

    def __init__(
        self, api_key: SecretStr, small_model: str, large_model: str, timeout_seconds: float
    ) -> None:
        super().__init__(small_model, large_model)
        self._api_key = api_key
        self._timeout = timeout_seconds

    def _build(self, model: str, temperature: float, max_tokens: int) -> BaseChatModel:
        return ChatGoogleGenerativeAI(
            model=model,
            api_key=self._api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=self._timeout,
            retries=1,  # "attempts including the first": one try, our client retries
        )

    async def generate(self, request: LLMRequest) -> ProviderReply:
        # langchain-google-genai maps Google errors to LangChain's ModelError types.
        # `from None` for the same reason as Groq: SDK errors can carry response content.
        try:
            return await self._invoke(request)
        except ModelRateLimitError:
            raise LLMRateLimitError("Gemini rate limit reached (HTTP 429).") from None
        except ModelError as exc:
            if exc.is_retryable:
                raise LLMTransientError(
                    f"Gemini temporary failure ({type(exc).__name__})."
                ) from None
            raise LLMRequestError(f"Gemini rejected the request ({type(exc).__name__}).") from None
        except httpx.TransportError as exc:  # timeouts and connection failures
            raise LLMTransientError(f"Gemini connection failed ({type(exc).__name__}).") from None


# ---------------------------------------------------------------- factory


def build_providers(settings: Settings) -> list[ChatProvider]:
    """The providers to try, in order. Raises LLMConfigError if none can be built."""
    if settings.llm_provider == "fake":
        return [FakeProvider([])]

    if settings.groq_api_key is None:
        raise LLMConfigError(
            "GROQ_API_KEY is not set. Add it to .env, or set LLM_PROVIDER=fake for offline use."
        )
    providers: list[ChatProvider] = [
        GroqProvider(
            settings.groq_api_key,
            settings.groq_small_model,
            settings.groq_large_model,
            settings.llm_timeout_seconds,
            settings.groq_reasoning_effort,
        )
    ]
    if settings.gemini_api_key is not None:  # the fallback is optional
        providers.append(
            GeminiProvider(
                settings.gemini_api_key,
                settings.gemini_small_model,
                settings.gemini_large_model,
                settings.llm_timeout_seconds,
            )
        )
    return providers


def build_llm_client(settings: Settings) -> RoutedLLMClient:
    return RoutedLLMClient(
        build_providers(settings),
        RetrySettings(
            max_attempts=settings.llm_max_attempts,
            backoff_base_seconds=settings.llm_backoff_base_seconds,
            backoff_max_seconds=settings.llm_backoff_max_seconds,
        ),
        default_temperature=settings.llm_temperature,
        default_max_tokens=settings.llm_max_tokens,
    )
