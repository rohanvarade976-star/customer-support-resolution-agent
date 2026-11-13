"""An LLM client that refuses every call.

Resuming a run after a reviewer's decision must run fixed code only: check the stored
decision, then execute, reopen or escalate (workflow/nodes/approval.py). The decision
endpoint gives the graph this client instead of a real one, so a model call there is
impossible by construction, not just by convention. If one ever happened it would be a bug,
so it fails loudly and the run is marked failed.
"""

from pydantic import BaseModel

from support_agent.llm.types import ChatMessage, LLMResult, ModelTier, ToolSpec


class ModelCallNotAllowedError(RuntimeError):
    pass


class DisabledLLMClient:
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tier: ModelTier = "small",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        raise ModelCallNotAllowedError("no model call is allowed here")

    async def structured[S: BaseModel](
        self,
        messages: list[ChatMessage],
        schema: type[S],
        *,
        tier: ModelTier = "small",
    ) -> S:
        raise ModelCallNotAllowedError("no model call is allowed here")
