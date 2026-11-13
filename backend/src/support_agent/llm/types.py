"""The data the LLM layer passes around: messages, tool definitions and results.

These are our own types, not LangChain's. Agents and tests only ever see these;
the provider adapters (providers.py) convert to and from LangChain at the edge.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

# "small": fast, cheap model for simple steps (classifier).
# "large": stronger model for reasoning (investigator, resolution).
ModelTier = Literal["small", "large"]

Role = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """One tool the model asked us to run. We decide whether and how to run it."""

    id: str
    name: str
    args: dict[str, Any]


class ChatMessage(BaseModel):
    role: Role
    content: str = ""
    # Only on assistant messages: the tool calls the model made in that turn.
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # Only on tool messages: which tool call this message answers.
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _check_role_fields(self) -> Self:
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages can carry tool calls")
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("tool messages (and only tool messages) need a tool_call_id")
        return self


def system_message(content: str) -> ChatMessage:
    """Our own trusted instructions. Never put customer text or retrieved documents here."""
    return ChatMessage(role="system", content=content)


def user_message(content: str) -> ChatMessage:
    """Untrusted input: customer text, retrieved documents, tool results shown as data."""
    return ChatMessage(role="user", content=content)


def customer_turn(system_prompt: str, customer_text: str) -> list[ChatMessage]:
    """The standard opening: our instructions as system, the customer's text as user.

    Keeping customer text out of the system message means it is treated as data to
    analyse, not as instructions with system-level authority (prompt injection defence).
    """
    return [system_message(system_prompt), user_message(customer_text)]


def check_message_order(messages: list[ChatMessage]) -> None:
    """Reject conversations where a system message appears anywhere but first.

    A system message later in the list usually means someone built one from data
    (for example, customer text) by mistake.
    """
    if not messages:
        raise ValueError("at least one message is required")
    for index, message in enumerate(messages):
        if message.role == "system" and index != 0:
            raise ValueError("a system message is only allowed as the first message")


class ToolSpec(BaseModel):
    """A tool the model may call: name, description and a JSON Schema for its arguments."""

    name: str
    description: str
    parameters: dict[str, Any]

    @classmethod
    def from_model(cls, name: str, description: str, model: type[BaseModel]) -> Self:
        return cls(name=name, description=description, parameters=model.model_json_schema())


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0  # includes reasoning tokens
    # Hidden "thinking" tokens (reasoning models such as gpt-oss). Already counted in
    # output_tokens; reported separately because they use up max_tokens.
    reasoning_tokens: int = 0


class LLMResult(BaseModel):
    """What `LLMClient.chat` returns."""

    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    provider: str  # which provider actually answered ("groq", "gemini", "fake")
    model: str
    usage: Usage


@dataclass(frozen=True)
class LLMRequest:
    """Everything a provider needs for one call. Built by the client, not by agents."""

    messages: list[ChatMessage]
    tier: ModelTier
    temperature: float
    max_tokens: int
    tools: list[ToolSpec] = field(default_factory=list)
    # The name of a tool the model must call (structured output), or None for free choice.
    forced_tool: str | None = None


@dataclass(frozen=True)
class ProviderReply:
    """One raw answer from a provider adapter, before the client adds bookkeeping."""

    content: str
    tool_calls: list[ToolCall]
    usage: Usage
