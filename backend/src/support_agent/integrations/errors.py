"""Errors raised by every external provider client (payments, shipping, ticketing).

Callers catch these, never httpx exceptions, so the rest of the app does not depend on
how we talk HTTP. Each error says which provider failed and carries the provider's
`error_code` (e.g. "REFUND_NOT_FOUND") so code can branch on it.
"""


class IntegrationError(Exception):
    """Base class: a call to an external provider did not succeed."""

    def __init__(self, provider: str, error_code: str, message: str) -> None:
        super().__init__(f"[{provider}] {error_code}: {message}")
        self.provider = provider
        self.error_code = error_code
        self.message = message


class ProviderUnavailableError(IntegrationError):
    """Temporary failure: timeout, connection failure or a 5xx response. Retryable.

    `request_sent` is False only when we know the provider never received the request
    (we could not even connect). When it is True the provider *may* have acted on it,
    which matters for writes that move money (see payments.create_refund).
    """

    def __init__(self, provider: str, error_code: str, message: str, *, request_sent: bool) -> None:
        super().__init__(provider, error_code, message)
        self.request_sent = request_sent


class ProviderRejectedError(IntegrationError):
    """The provider refused the request (4xx other than 404). Retrying gives the same answer."""

    def __init__(self, provider: str, status_code: int, error_code: str, message: str) -> None:
        super().__init__(provider, error_code, message)
        self.status_code = status_code


class ResourceNotFoundError(IntegrationError):
    """The provider has no such resource (404).

    Kept separate from ProviderRejectedError because reconciliation relies on it:
    "no refund exists for this idempotency key" is a definite, useful answer.
    """


class OutcomeUnknownError(IntegrationError):
    """A write was sent but we never got a clear answer, so it may or may not have happened.

    Never retry blindly. Ask the provider what happened, using the idempotency key.
    """

    def __init__(self, provider: str, idempotency_key: str, message: str) -> None:
        super().__init__(provider, "OUTCOME_UNKNOWN", message)
        self.idempotency_key = idempotency_key
