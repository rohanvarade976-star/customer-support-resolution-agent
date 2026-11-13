"""The investigator agent: plan, look up records with read tools, summarise the evidence.

Three steps, all on the large model (PRD "Agent specifications", FR-4):

1. Plan: one structured call. A short goal and ordered steps.
2. Tool loop (llm/tool_loop.py): the model calls read tools one at a time, up to the step
   limit. It may change course when a result surprises it.
3. Evidence summary: one structured call. Every fact cites the call id it came from.

The model only proposes. Code decides what counts:
- `INVESTIGATOR_TOOLS` is the allowlist: read tools only, no `search_policy`, no writes.
- `ground_facts` drops every fact whose call id did not happen or returned an error, so
  an invented or error-based "fact" never reaches later agents. It does not judge the
  wording of a fact, only that a successful call exists behind it.

LLM failures do not raise: they come back as `InvestigationOutcome.failure`, together
with whatever trace exists so far, and the workflow escalates (nodes/investigate.py).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from support_agent.llm.client import LLMClient
from support_agent.llm.errors import LLMUnavailableError, StructuredOutputError, ToolLoopOutputError
from support_agent.llm.tool_loop import (
    OUTCOME_OK,
    ToolCallRecord,
    ToolLoop,
    ToolResult,
    wrap_untrusted,
)
from support_agent.llm.types import ChatMessage, system_message, user_message
from support_agent.tools.base import ToolContext, ToolRegistry

PROMPT_PATH = Path(__file__).parent / "prompts" / "investigator.md"

# The only tools this agent may use. Read-only: no search_policy (the policy agent's tool)
# and nothing that writes.
INVESTIGATOR_TOOLS: tuple[str, ...] = (
    "get_order",
    "get_return_status",
    "get_payment_status",
    "get_shipment",
    "get_refundable_balance",
    "check_refund_eligibility",
)

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=300)]

_PLAN_INSTRUCTION = (
    "Step 1. Write your investigation plan now. Do not call any tool yet. "
    "Use the order number above."
)
_EXECUTE_INSTRUCTION = (
    "Step 2. Carry out your plan with the tools, one call at a time. "
    "When you are finished, reply with a short note and no tool call."
)
_SUMMARY_INSTRUCTION = (
    "Step 3. Write the evidence summary now. Cite each fact's call_id exactly as it "
    "appears in the tool results."
)


class InvestigationPlan(BaseModel):
    goal: ShortText = Field(description="One sentence: what we need to find out.")
    steps: list[ShortText] = Field(
        min_length=1, max_length=8, description="Ordered steps: which record, and why."
    )


class Fact(BaseModel):
    statement: ShortText = Field(description="One plain statement taken from a tool result.")
    call_id: str = Field(
        min_length=1, max_length=100, description="The call_id of the tool result it came from."
    )


class EvidenceSummary(BaseModel):
    """What the model must return in step 3. Not trusted until `ground_facts` has checked it."""

    facts: list[Fact] = Field(max_length=20)
    missing_information: list[ShortText] = Field(default_factory=list, max_length=10)
    plan_changes: list[ShortText] = Field(default_factory=list, max_length=5)


class Evidence(BaseModel):
    """The summary after the grounding check: `facts` all rest on a successful tool call."""

    facts: list[Fact]
    dropped_facts: list[Fact]  # flagged: cited a call that did not happen or failed
    missing_information: list[str]
    plan_changes: list[str]


class InvestigationFailure(StrEnum):
    """Why an investigation stopped early. Values match WorkflowError codes."""

    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    OUTPUT_INVALID = "INVESTIGATOR_OUTPUT_INVALID"
    STEP_LIMIT = "INVESTIGATION_STEP_LIMIT"


@dataclass(frozen=True)
class InvestigationOutcome:
    plan: InvestigationPlan | None  # None if the planning call itself failed
    trace: list[ToolCallRecord]  # what ran so far, even on failure
    evidence: Evidence | None  # None on failure
    failure: InvestigationFailure | None
    # Structured outputs of the successful calls, so code can compare real data (e.g. dates
    # in two systems) instead of fact sentences. Holds customer data: in memory only, never
    # store or log it. The content-free `trace` is the part to store.
    results: list[ToolResult] = field(default_factory=list)


@cache
def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def ground_facts(summary: EvidenceSummary, trace: list[ToolCallRecord]) -> Evidence:
    """Keep only facts that cite a tool call that really happened and succeeded."""
    succeeded = {record.call_id for record in trace if record.outcome == OUTCOME_OK}
    kept = [fact for fact in summary.facts if fact.call_id in succeeded]
    dropped = [fact for fact in summary.facts if fact.call_id not in succeeded]
    return Evidence(
        facts=kept,
        dropped_facts=dropped,
        missing_information=summary.missing_information,
        plan_changes=summary.plan_changes,
    )


def _case_message(complaint_text: str, order_number: str, focus: str | None) -> ChatMessage:
    # The order number is verified by code before the investigator runs (classify node).
    # The complaint and the supervisor's hint are untrusted text.
    text = f"Order number for this case: {order_number}\n\n" + wrap_untrusted(
        "customer_complaint", complaint_text
    )
    if focus:
        text += "\n\n" + wrap_untrusted("supervisor_hint", focus)
    return user_message(text)


def _plan_text(plan: InvestigationPlan) -> str:
    steps = "\n".join(f"{number}. {step}" for number, step in enumerate(plan.steps, start=1))
    return f"My plan.\nGoal: {plan.goal}\n{steps}"


async def investigate(
    llm: LLMClient,
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    complaint_text: str,
    order_number: str,
    max_tool_calls: int,
    focus: str | None = None,
) -> InvestigationOutcome:
    """Run plan -> tool loop -> evidence. `registry` is cut down to INVESTIGATOR_TOOLS here."""
    loop = ToolLoop(
        llm, registry.restricted(INVESTIGATOR_TOOLS), ctx, max_calls=max_tool_calls, tier="large"
    )
    base = [system_message(load_prompt()), _case_message(complaint_text, order_number, focus)]
    plan: InvestigationPlan | None = None
    try:
        plan = await llm.structured(
            [*base, user_message(_PLAN_INSTRUCTION)], InvestigationPlan, tier="large"
        )
        looped = await loop.run(
            [
                *base,
                ChatMessage(role="assistant", content=_plan_text(plan)),
                user_message(_EXECUTE_INSTRUCTION),
            ]
        )
        if looped.step_limit_reached:
            return InvestigationOutcome(
                plan, list(loop.trace), None, InvestigationFailure.STEP_LIMIT, list(loop.results)
            )
        summary = await llm.structured(
            [*looped.messages, user_message(_SUMMARY_INSTRUCTION)], EvidenceSummary, tier="large"
        )
    except LLMUnavailableError:
        return InvestigationOutcome(
            plan, list(loop.trace), None, InvestigationFailure.LLM_UNAVAILABLE, list(loop.results)
        )
    except (StructuredOutputError, ToolLoopOutputError):
        return InvestigationOutcome(
            plan, list(loop.trace), None, InvestigationFailure.OUTPUT_INVALID, list(loop.results)
        )
    trace = list(loop.trace)
    return InvestigationOutcome(plan, trace, ground_facts(summary, trace), None, list(loop.results))
