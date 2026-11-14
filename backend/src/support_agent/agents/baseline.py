"""The single-agent baseline (PRD FR-23): one agent with every read tool, for comparison.

    gather:  one tool loop with all the read tools plus `search_policy`
    (the workflow then runs the same required evidence and computed facts as multi-agent)
    decide:  one structured call for the same `ResolutionDraft`, then the same validator

What it does not have, on purpose: no supervisor, no separate investigator or policy agent,
no policy-findings step, no critic and no revision. Those are what the multi-agent design
adds, so the comparison shows what they are worth. What it shares with multi-agent is
everything that decides: eligibility and delay facts computed by code, the required
evidence, `validate_resolution`, and the rule that only code sets a refund amount.

Policy rules for the validator come from the passages its own searches returned (a citation
is grounded only if a search returned it). The baseline has no conflict check, so
`conflicting_passages` is always False; the validator's other conflict check (order versus
carrier delivery time) still runs.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from functools import cache
from pathlib import Path

from support_agent.agents.investigator import INVESTIGATOR_TOOLS
from support_agent.agents.policy import (
    POLICY_TOOLS,
    PolicyFindings,
    PolicyFindingsDraft,
    PolicyRuleDraft,
    ground_rules,
)
from support_agent.agents.resolution import (
    ResolutionInputs,
    draft_resolution,
    render_case,
)
from support_agent.agents.resolution import load_prompt as load_resolution_prompt
from support_agent.agents.resolve import (
    Attempt,
    ResolutionFailure,
    ResolutionOutcome,
    build_context,
    escalation_outcome,
    final_outcome,
    normalize_draft,
    to_claim,
)
from support_agent.domain.validation import validate_resolution
from support_agent.llm.client import LLMClient
from support_agent.llm.errors import LLMUnavailableError, StructuredOutputError, ToolLoopOutputError
from support_agent.llm.tool_loop import ToolCallRecord, ToolLoop, ToolResult, wrap_untrusted
from support_agent.llm.types import ChatMessage, system_message, user_message
from support_agent.schemas.policies import PolicySearchResult
from support_agent.tools.base import ToolContext, ToolRegistry

PROMPT_PATH = Path(__file__).parent / "prompts" / "baseline.md"

BASELINE_TOOLS: tuple[str, ...] = (*INVESTIGATOR_TOOLS, *POLICY_TOOLS)

_RULE_TEXT_LIMIT = 500  # the policy agent's rule statement limit (ADR-009)

_GATHER_INSTRUCTION = (
    "Gather what you need with the tools, one call at a time. "
    "When you are finished, reply with a short note and no tool call."
)
_DRAFT_INSTRUCTION = "Write the resolution now."
_RESULTS_HEADING = "What your tool calls returned (each result names its call_id; cite it):"


class BaselineFailure(StrEnum):
    """Why gathering stopped early. Values match WorkflowError codes."""

    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    OUTPUT_INVALID = "BASELINE_OUTPUT_INVALID"
    STEP_LIMIT = "BASELINE_STEP_LIMIT"


@dataclass(frozen=True)
class BaselineGathering:
    # The conversation so far, tool exchanges included: the draft call continues from it.
    # Holds customer data: in memory only.
    messages: list[ChatMessage]
    trace: list[ToolCallRecord]  # content-free
    results: list[ToolResult]  # in memory only
    failure: BaselineFailure | None


@cache
def load_prompt() -> str:
    """The baseline's own instructions followed by the resolution agent's writing rules."""
    return PROMPT_PATH.read_text(encoding="utf-8").strip() + "\n\n" + load_resolution_prompt()


async def gather(
    llm: LLMClient,
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    complaint_text: str,
    category: str,
    order_number: str,
    max_tool_calls: int,
) -> BaselineGathering:
    loop = ToolLoop(
        llm, registry.restricted(BASELINE_TOOLS), ctx, max_calls=max_tool_calls, tier="large"
    )
    case = user_message(
        f"Order number for this case: {order_number}\nComplaint category: {category}\n\n"
        + wrap_untrusted("customer_complaint", complaint_text)
    )
    start = [system_message(load_prompt()), case, user_message(_GATHER_INSTRUCTION)]
    failure: BaselineFailure | None = None
    messages = start
    try:
        looped = await loop.run(start)
        messages = looped.messages
        if looped.step_limit_reached:
            failure = BaselineFailure.STEP_LIMIT
    except LLMUnavailableError:
        failure = BaselineFailure.LLM_UNAVAILABLE
    except ToolLoopOutputError:
        failure = BaselineFailure.OUTPUT_INVALID
    return BaselineGathering(messages, list(loop.trace), list(loop.results), failure)


def grounded_findings(results: list[ToolResult]) -> PolicyFindings:
    """Policy rules for the validator: every passage this run's searches returned."""
    passages = {
        passage.citation: passage
        for result in results
        if isinstance(result.output, PolicySearchResult)
        for passage in result.output.passages
    }
    rules = [
        PolicyRuleDraft(
            statement=passage.content.strip()[:_RULE_TEXT_LIMIT] or passage.section_title,
            citation=citation,
        )
        for citation, passage in passages.items()
    ]
    draft = PolicyFindingsDraft(rules=rules, no_relevant_policy=False, conflicting_passages=False)
    return ground_rules(draft, results)


def draft_messages(gathering: BaselineGathering, inputs: ResolutionInputs) -> list[ChatMessage]:
    """The draft call's conversation: no tool-call history, the results as plain text.

    The draft call binds only `resolution_draft`. Continuing the gathering conversation
    would replay calls to tools that are not bound, which Gemini answers with plain text
    instead of the forced call (ADR-016). Each result keeps its call_id and its
    untrusted-data wrapper, so the draft can still cite it and the validator still checks it.
    """
    results = "\n\n".join(m.content for m in gathering.messages if m.role == "tool")
    request = "\n\n".join(
        [_RESULTS_HEADING, results or "(no tool call succeeded)", render_case(inputs)]
    )
    return [system_message(load_prompt()), user_message(request + "\n\n" + _DRAFT_INSTRUCTION)]


async def decide(
    llm: LLMClient,
    gathering: BaselineGathering,
    inputs: ResolutionInputs,
    *,
    trace: list[ToolCallRecord],
    results: list[ToolResult],
    delivery_tolerance: timedelta,
) -> ResolutionOutcome:
    """One draft, then the shared validator. No critic and no revision."""
    ctx = build_context(inputs, trace, results, delivery_tolerance)
    messages = draft_messages(gathering, inputs)
    try:
        draft = normalize_draft(await draft_resolution(llm, messages))
    except LLMUnavailableError:
        unavailable = ResolutionFailure.LLM_UNAVAILABLE
        return escalation_outcome([unavailable.value], [], failure=unavailable)
    except StructuredOutputError:
        invalid = ResolutionFailure.RESOLUTION_OUTPUT_INVALID
        return escalation_outcome([invalid.value], [], failure=invalid)
    codes = validate_resolution(to_claim(draft, inputs), ctx)
    return final_outcome(draft, inputs, ctx, [Attempt(draft, codes, None)])
