"""Idempotency-Key header checks shared by every write route.

A route declares the header and calls `check_idempotency_key` first thing, so body
validation errors (422) still come before a missing key (400).
"""

from typing import Annotated

from fastapi import Header

from mock_providers.common.errors import ProviderError

MAX_KEY_LENGTH = 255

# Route parameter type: `idempotency_key: IdempotencyKeyHeader = None`
IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


def check_idempotency_key(key: str | None) -> str:
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ProviderError(
            400,
            "IDEMPOTENCY_KEY_MISSING",
            f"Idempotency-Key header (1-{MAX_KEY_LENGTH} chars) is required.",
            retryable=False,
        )
    return key


def key_reused_error() -> ProviderError:
    """Same key, different request body: the caller has a bug, so retrying will not help."""
    return ProviderError(
        409,
        "IDEMPOTENCY_KEY_REUSED",
        "This Idempotency-Key was already used with a different request body.",
        retryable=False,
    )
