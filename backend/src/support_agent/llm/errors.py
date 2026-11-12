"""Errors raised by the LLM layer.

Provider adapters translate every SDK error into one of these, so the rest of the app
never needs to know which SDK (Groq, Gemini) was used. Messages never contain prompt
or response content, because SDK error bodies can echo what the model generated.

Which errors are retried (see client.py):
- LLMRateLimitError, LLMTransientError: retried with backoff, then fall back to the
  next provider.
- Everything else: raised immediately (retrying the same request gives the same answer).
"""


class LLMError(Exception):
    """Base class for every LLM-layer error."""


class LLMConfigError(LLMError):
    """The LLM layer is not configured correctly (for example, a missing API key)."""


class LLMRateLimitError(LLMError):
    """The provider said "too many requests". Safe to retry after waiting."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        # The provider's own hint (Retry-After header), if it gave one.
        self.retry_after_seconds = retry_after_seconds


class LLMTransientError(LLMError):
    """A timeout, dropped connection or provider 5xx. Safe to retry."""


class LLMRequestError(LLMError):
    """The provider rejected the request itself (bad request, bad key, unknown model).

    Not retried and no fallback: this is a bug or a setup problem to fix.
    """


class LLMInvalidOutputError(LLMError):
    """The model answered, but its tool call arguments were not valid JSON."""


class LLMUnavailableError(LLMError):
    """Every configured provider failed with rate limits or transient errors."""


class StructuredOutputError(LLMError):
    """The model's structured output was still invalid after one repair attempt."""

    def __init__(self, schema_name: str, errors: list[str]) -> None:
        # `errors` hold field locations and rule messages only, never the model's values.
        super().__init__(
            f"Model output for {schema_name} was invalid after one repair attempt: "
            + "; ".join(errors)
        )
        self.schema_name = schema_name
        self.errors = errors
