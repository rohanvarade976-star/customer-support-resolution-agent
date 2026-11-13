"""Structured output: get an answer shaped like a Pydantic model, or a clear error.

How it works:
1. The schema is offered to the model as one tool, and the model is *required* to call
   it. The tool-call arguments are the answer. This works the same way on Groq and
   Gemini and on every tool-capable model (ADR-007).
2. We validate the arguments with Pydantic. The model's word is never trusted unchecked.
3. If they are invalid, we ask exactly once more, listing what was wrong.
4. If the second answer is also invalid, we raise StructuredOutputError.

The repair message lists field locations and rule messages only. The invalid values are
left out (`include_input=False`), so customer data is not echoed back into the prompt.
"""

import re
from typing import Protocol

from pydantic import BaseModel, ValidationError

from support_agent.llm.errors import LLMInvalidOutputError, StructuredOutputError
from support_agent.llm.types import (
    ChatMessage,
    LLMRequest,
    LLMResult,
    ModelTier,
    ToolSpec,
    user_message,
)


class _Completer(Protocol):
    """The part of RoutedLLMClient this module needs (avoids a circular import)."""

    def request_defaults(self) -> tuple[float, int]: ...

    async def complete(self, request: LLMRequest) -> LLMResult: ...


async def generate_structured[S: BaseModel](
    client: _Completer,
    messages: list[ChatMessage],
    schema: type[S],
    *,
    tier: ModelTier,
) -> S:
    tool = ToolSpec.from_model(
        name=tool_name_for(schema),
        description=f"Return your answer as a {schema.__name__}.",
        model=schema,
    )
    temperature, max_tokens = client.request_defaults()

    def request_for(conversation: list[ChatMessage]) -> LLMRequest:
        return LLMRequest(
            messages=conversation,
            tier=tier,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=[tool],
            forced_tool=tool.name,
        )

    first = await _try_once(client, request_for(messages), schema)
    if isinstance(first, BaseModel):
        return first

    # One repair attempt: same conversation plus a note on what was wrong.
    repair = user_message(
        f"Your previous answer did not match the required format. Problems: "
        f"{'; '.join(first)}. Call the {tool.name} tool again with corrected arguments."
    )
    second = await _try_once(client, request_for([*messages, repair]), schema)
    if isinstance(second, BaseModel):
        return second
    raise StructuredOutputError(schema.__name__, second)


def tool_name_for(schema: type[BaseModel]) -> str:
    """snake_case tool name for a schema, e.g. TriageResult -> triage_result.

    Models are trained on snake_case tool names; gpt-oss rewrote "Triage" as "triage",
    which Groq then rejected as an unknown tool.
    """
    return re.sub(r"(?<!^)(?=[A-Z])", "_", schema.__name__).lower()


async def _try_once[S: BaseModel](
    client: _Completer, request: LLMRequest, schema: type[S]
) -> S | list[str]:
    """Return the validated model, or a list of readable problems."""
    try:
        result = await client.complete(request)
    except LLMInvalidOutputError:
        return [
            f"the tool call was malformed; call {request.forced_tool} with valid JSON arguments"
        ]

    call = next((c for c in result.tool_calls if c.name == request.forced_tool), None)
    if call is None:
        return [f"you must call the {request.forced_tool} tool"]
    try:
        return schema.model_validate(call.args)
    except ValidationError as exc:
        return _describe(exc)


def _describe(exc: ValidationError) -> list[str]:
    """Field location and rule, e.g. "confidence: Input should be less than or equal to 1"."""
    problems = []
    for error in exc.errors(include_input=False, include_url=False):
        where = ".".join(str(part) for part in error["loc"]) or "(root)"
        problems.append(f"{where}: {error['msg']}")
    return problems
