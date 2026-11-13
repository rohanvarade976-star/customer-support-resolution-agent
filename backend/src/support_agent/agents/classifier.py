"""The classifier agent: one structured LLM call that labels a complaint (PRD FR-3).

It only *proposes* a label. The workflow decides what to do with it (graph.py), and any
order number it extracts is a hint that code verifies before use (nodes/classify.py).

The prompt lives in prompts/classifier.md. Everything after the `<!-- examples -->`
marker is the few-shot examples; `few_shot=False` cuts them off so the eval script can
compare the two prompts.
"""

from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from support_agent.llm.client import LLMClient
from support_agent.llm.types import customer_turn

PROMPT_PATH = Path(__file__).parent / "prompts" / "classifier.md"
EXAMPLES_MARKER = "<!-- examples -->"

# The three real ticket categories plus "unclear" (no clear category -> clarification).
ClassifierCategory = Literal["order_status", "refund", "damaged_item", "unclear"]


class Classification(BaseModel):
    """What the model must return. Pydantic validates it; the model's word is not trusted."""

    category: ClassifierCategory
    confidence: float = Field(ge=0, le=1, description="How sure the label is, 0 to 1.")
    # Loose on purpose: "1042" or "#1042" must not fail validation and escalate the run.
    # Code normalises and verifies it (nodes/classify.py).
    order_number: str | None = Field(
        default=None, max_length=40, description="Order number the customer wrote, if any."
    )
    reason: str = Field(max_length=300, description="One short sentence.")


@cache
def load_prompt(*, few_shot: bool = True) -> str:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    if few_shot:
        return text.replace(EXAMPLES_MARKER, "").strip()
    return text.split(EXAMPLES_MARKER)[0].strip()


async def classify(llm: LLMClient, complaint_text: str, *, few_shot: bool = True) -> Classification:
    """Label one complaint. Raises LLMUnavailableError or StructuredOutputError on failure.

    The complaint is always the user message, never part of the system prompt.
    """
    messages = customer_turn(load_prompt(few_shot=few_shot), complaint_text)
    return await llm.structured(messages, Classification, tier="small")
