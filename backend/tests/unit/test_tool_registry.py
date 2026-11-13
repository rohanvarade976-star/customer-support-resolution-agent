"""Registry behaviour that needs no database: registration rules, error mapping, logging."""

import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.integrations.errors import ProviderRejectedError, ProviderUnavailableError
from support_agent.integrations.shipping import ShippingClient
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolFailureError,
    ToolRegistry,
)


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


class Out(BaseModel):
    doubled: int


class LooseIn(BaseModel):  # extra not forbidden
    value: int


class WithCustomerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str


def make_ctx() -> ToolContext:
    settings = Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+asyncpg://u:p@localhost/unused"),
        jwt_secret=SecretStr("x" * 32),
    )
    return ToolContext(
        session=MagicMock(spec=AsyncSession),
        customer_id=uuid4(),
        settings=settings,
        shipping_client=MagicMock(spec=ShippingClient),
        embedder=FakeEmbedder(),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )


def registry_with(handler: object) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool("t", "A test tool.", In, Out, handler))  # type: ignore[arg-type]
    return registry


async def double(_ctx: ToolContext, args: In) -> Out:
    return Out(doubled=args.value * 2)


async def test_success_returns_the_output_model() -> None:
    result = await registry_with(double).execute("t", make_ctx(), {"value": 4})

    assert result == Out(doubled=8)


async def test_unknown_tool_and_bad_input_are_structured_errors() -> None:
    registry = registry_with(double)

    unknown = await registry.execute("nope", make_ctx(), {})
    bad_type = await registry.execute("t", make_ctx(), {"value": "secret-text"})
    extra = await registry.execute("t", make_ctx(), {"value": 1, "customer_id": "abc"})

    assert isinstance(unknown, ToolError) and unknown.error_code == "UNKNOWN_TOOL"
    assert isinstance(bad_type, ToolError) and bad_type.error_code == "INVALID_INPUT"
    assert "secret-text" not in bad_type.message  # the input value is never echoed back
    assert isinstance(extra, ToolError) and extra.error_code == "INVALID_INPUT"
    assert extra.retryable is False and extra.success is False


@pytest.mark.parametrize(
    ("raised", "code", "retryable"),
    [
        (ToolFailureError("RETURN_NOT_RECEIVED", "Not received."), "RETURN_NOT_RECEIVED", False),
        (AppError(404, "ORDER_NOT_FOUND", "Order not found."), "ORDER_NOT_FOUND", False),
        (
            ProviderUnavailableError("shipping", "TIMEOUT", "slow", request_sent=True),
            "SHIPPING_UNAVAILABLE",
            True,
        ),
        (ProviderRejectedError("shipping", 400, "BAD", "nope"), "SHIPPING_ERROR", False),
    ],
)
async def test_expected_failures_become_structured_errors(
    raised: Exception, code: str, retryable: bool
) -> None:
    async def fail(_ctx: ToolContext, _args: In) -> Out:
        raise raised

    result = await registry_with(fail).execute("t", make_ctx(), {"value": 1})

    assert isinstance(result, ToolError)
    assert (result.error_code, result.retryable) == (code, retryable)


async def test_unexpected_exception_is_generic_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def explode(_ctx: ToolContext, _args: In) -> Out:
        raise RuntimeError("password=hunter2 host=db.internal")

    with caplog.at_level(logging.ERROR):
        result = await registry_with(explode).execute("t", make_ctx(), {"value": 1})

    assert result == ToolError(
        error_code="INTERNAL_ERROR", retryable=False, message="Something went wrong."
    )
    assert "hunter2" not in result.model_dump_json()
    logged = [record for record in caplog.records if record.exc_info is not None]
    assert len(logged) == 1  # the real exception is in the log
    assert "hunter2" in str(logged[0].exc_info)


def test_registration_rules() -> None:
    registry = registry_with(double)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(Tool("t", "dup", In, Out, double))
    with pytest.raises(ValueError, match="extra='forbid'"):
        registry.register(Tool("loose", "x", LooseIn, Out, double))
    with pytest.raises(ValueError, match="customer_id"):
        registry.register(Tool("who", "x", WithCustomerIn, Out, double))


async def test_restricted_registry_holds_only_the_allowed_tools() -> None:
    registry = registry_with(double)
    registry.register(Tool("other", "Another tool.", In, Out, double))

    subset = registry.restricted(["other"])

    assert subset.names == ["other"]
    assert [schema["name"] for schema in subset.export_schemas()] == ["other"]
    excluded = await subset.execute("t", make_ctx(), {"value": 1})
    assert isinstance(excluded, ToolError) and excluded.error_code == "UNKNOWN_TOOL"
    assert registry.names == ["t", "other"]  # the original is unchanged


def test_restricted_registry_rejects_unregistered_names() -> None:
    with pytest.raises(ValueError, match="unregistered tools"):
        registry_with(double).restricted(["t", "nope"])
