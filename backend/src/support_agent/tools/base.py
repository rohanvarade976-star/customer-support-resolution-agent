"""Tool plumbing: the context a tool runs in, the structured error, and the registry.

A tool is a typed function an agent may call. The model supplies only the arguments in
the tool's input model. Who the customer is comes from `ToolContext.customer_id`, which
is set from the authenticated run, so no input model may have a customer field.

`ToolRegistry.execute` never raises: it returns the tool's output model or a `ToolError`.
"""

import logging
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.integrations.errors import IntegrationError, ProviderUnavailableError
from support_agent.integrations.shipping import ShippingClient
from support_agent.rag.embeddings import Embedder

log = logging.getLogger(__name__)

# No input model may carry identity: it comes from the context, never from the model.
FORBIDDEN_INPUT_FIELDS = frozenset({"customer_id", "customer"})

# The only order reference a tool accepts: the human-readable order number, never a UUID.
ORDER_NUMBER_PATTERN = r"^ORD-[0-9]+$"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool may use. Built by code for one run; never by the model."""

    session: AsyncSession
    customer_id: UUID  # from the authenticated JWT
    settings: Settings
    shipping_client: ShippingClient
    embedder: Embedder  # the process-wide cached embedder (rag.embeddings.get_embedder)
    clock: Callable[[], datetime] = _utc_now  # tests inject a fixed time


class ToolError(BaseModel):
    """The structured failure format from the PRD "Tool contracts"."""

    success: Literal[False] = False
    error_code: str
    retryable: bool
    message: str


class ToolFailureError(Exception):
    """Raised by a handler to return a specific, expected ToolError."""

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error = ToolError(error_code=error_code, retryable=retryable, message=message)


@dataclass(frozen=True)
class Tool[I: BaseModel, O: BaseModel]:
    name: str
    description: str
    input_model: type[I]
    output_model: type[O]
    handler: Callable[[ToolContext, I], Awaitable[O]]


def _error_from_exception(tool_name: str, exc: Exception) -> ToolError:
    """Map any exception to a safe ToolError. Internal details never reach the caller."""
    if isinstance(exc, ToolFailureError):
        return exc.error
    if isinstance(exc, AppError):  # e.g. ORDER_NOT_FOUND from a service
        return ToolError(error_code=exc.error_code, retryable=False, message=exc.message)
    if isinstance(exc, IntegrationError):
        name = exc.provider.upper()
        if isinstance(exc, ProviderUnavailableError):
            return ToolError(
                error_code=f"{name}_UNAVAILABLE",
                retryable=True,
                message=f"The {exc.provider} service is temporarily unavailable.",
            )
        return ToolError(
            error_code=f"{name}_ERROR",
            retryable=False,
            message=f"The {exc.provider} service could not complete the request.",
        )
    # Unexpected: keep the real exception in our logs only.
    log.exception("tool failed unexpectedly", extra={"tool": tool_name})
    return ToolError(error_code="INTERNAL_ERROR", retryable=False, message="Something went wrong.")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool[Any, Any]] = {}

    def register(self, tool: Tool[Any, Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        if tool.input_model.model_config.get("extra") != "forbid":
            raise ValueError(f"{tool.name}: input model must set extra='forbid'")
        forbidden = FORBIDDEN_INPUT_FIELDS & set(tool.input_model.model_fields)
        if forbidden:
            raise ValueError(f"{tool.name}: input model must not have {sorted(forbidden)}")
        self._tools[tool.name] = tool

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def restricted(self, allowed: Collection[str]) -> "ToolRegistry":
        """A new registry holding only the allowed tools (an agent's allowlist).

        Raises ValueError for a name that is not registered, so a typo in an allowlist
        fails at start-up instead of silently giving an agent fewer (or other) tools.
        """
        unknown = set(allowed) - set(self._tools)
        if unknown:
            raise ValueError(f"cannot allow unregistered tools: {sorted(unknown)}")
        subset = ToolRegistry()
        for name in allowed:
            subset.register(self._tools[name])
        return subset

    async def execute(
        self, name: str, ctx: ToolContext, arguments: Mapping[str, Any]
    ) -> BaseModel | ToolError:
        tool = self._tools.get(name)
        if tool is None:
            return ToolError(
                error_code="UNKNOWN_TOOL", retryable=False, message=f"Unknown tool: {name}."
            )
        try:
            parsed = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            first = exc.errors()[0]  # location and rule only; never echo the input value back
            where = ".".join(str(part) for part in first["loc"]) or "input"
            return ToolError(
                error_code="INVALID_INPUT",
                retryable=False,
                message=f"{where}: {first['msg']}",
            )
        try:
            result: BaseModel = await tool.handler(ctx, parsed)
        except Exception as exc:
            return _error_from_exception(name, exc)
        return result

    def export_schemas(self) -> list[dict[str, Any]]:
        """JSON schemas for LLM tool calling: name, description, input and output schema."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_model.model_json_schema(),
                "output_schema": tool.output_model.model_json_schema(),
            }
            for tool in self._tools.values()
        ]
