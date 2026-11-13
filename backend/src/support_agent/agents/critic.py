"""The critic: a second model reads the draft and says approve or revise.

It checks the draft against the evidence and the policy rules (PRD FR-10). Its text is
untrusted for decisions:
- `CriticVerdict` has no amount and no action field, so it cannot change either.
- The verdict never overrides the validator (agents/resolve.py): an approval only lets a
  draft that already passed validation go on to the final validation.
- Its issues are only ever fed back to the drafting model, wrapped as untrusted data.
"""

from functools import cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from support_agent.agents.resolution import ResolutionDraft, ResolutionInputs, render_case
from support_agent.llm.client import LLMClient
from support_agent.llm.tool_loop import wrap_untrusted
from support_agent.llm.types import ChatMessage, ModelTier, system_message, user_message

PROMPT_PATH = Path(__file__).parent / "prompts" / "critic.md"

Issue = Annotated[str, StringConstraints(min_length=1, max_length=400)]


class CriticVerdict(BaseModel):
    verdict: Literal["approve", "revise"]
    issues: list[Issue] = Field(
        default_factory=list, max_length=5, description="Specific problems. Empty if approved."
    )


@cache
def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def _render_draft(draft: ResolutionDraft) -> str:
    return (
        f"action: {draft.action.value}\n"
        f"rationale: {draft.rationale}\n"
        f"cited call ids: {', '.join(draft.cited_call_ids) or '(none)'}\n"
        f"cited policy rules: {', '.join(draft.policy_citations) or '(none)'}\n"
        f"customer message:\n{draft.customer_message}"
    )


def review_messages(inputs: ResolutionInputs, draft: ResolutionDraft) -> list[ChatMessage]:
    return [
        system_message(load_prompt()),
        user_message(
            render_case(inputs)
            + "\n\n"
            + wrap_untrusted("draft_resolution", _render_draft(draft))
            + "\n\nReview the draft now."
        ),
    ]


async def review_draft(
    llm: LLMClient, inputs: ResolutionInputs, draft: ResolutionDraft, *, tier: ModelTier
) -> CriticVerdict:
    """One structured call. May raise the LLM errors (resolve.py handles them)."""
    return await llm.structured(review_messages(inputs, draft), CriticVerdict, tier=tier)
