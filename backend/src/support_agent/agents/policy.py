"""The policy agent: search the policies, then state the relevant rules with citations.

Two steps (PRD "Agent specifications", FR-5, FR-6):

1. Search: the tool loop (llm/tool_loop.py) lets the model call `search_policy`, refining
   its query, up to the search limit. Runs on the search tier (small by default).
2. Findings: one structured call on the findings tier (large by default, ADR-009). Each
   rule carries one citation (a chunk_key).

The model only proposes. Code decides what counts:
- `POLICY_TOOLS` is the allowlist: `search_policy` and nothing else, no read tools, no
  writes. The model is only offered that schema and the loop refuses any other name.
- `ground_rules` keeps a rule only if its citation is a chunk_key that appeared in this
  run's own search results. Other rules are dropped and flagged. It checks the citation,
  not the wording.
- The policy versions are taken from the kept citations, not from the model.
- `no_relevant_policy` is set by code whenever no grounded rule is left, so a run that
  found nothing (or only invented citations) can never look like it found policy.
- The agent states rules only. Eligibility and amounts are decided in code elsewhere.

LLM failures do not raise: they come back as `PolicyOutcome.failure`, with the searches
made so far, and the workflow escalates.
"""

from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from support_agent.llm.client import LLMClient
from support_agent.llm.errors import LLMUnavailableError, StructuredOutputError, ToolLoopOutputError
from support_agent.llm.tool_loop import ToolCallRecord, ToolLoop, ToolResult, wrap_untrusted
from support_agent.llm.types import ChatMessage, ModelTier, system_message, user_message
from support_agent.schemas.policies import PolicyPassage, PolicySearchResult
from support_agent.tools.base import ToolContext, ToolRegistry

PROMPT_PATH = Path(__file__).parent / "prompts" / "policy.md"

# The only tool this agent may use. Read-only, and the only way it sees policy text.
POLICY_TOOLS: tuple[str, ...] = ("search_policy",)

RuleText = Annotated[str, StringConstraints(min_length=1, max_length=500)]

_SEARCH_INSTRUCTION = (
    "Step 1. Search the policies for the rules that matter here. "
    "Use the order number above in every search."
)
# The field names are spelled out: a model that is only told "write the findings" may
# invent its own key (it wrote "text" for "statement"), which the provider rejects (ADR-009).
_FINDINGS_INSTRUCTION = (
    "Step 2. Write the findings now. Each rule is an object with exactly two keys: "
    "`statement` (the rule in your own short words) and `citation` (copied exactly as it "
    "appears in the search results). State rules only: no eligibility decisions and no amounts."
)


class PolicyRuleDraft(BaseModel):
    statement: RuleText = Field(description="One short rule, taken from a search result.")
    citation: str = Field(
        min_length=1,
        max_length=200,
        description="The citation (chunk_key) of the passage it came from, copied exactly.",
    )


class PolicyFindingsDraft(BaseModel):
    """What the model must return in step 2. Not trusted until `ground_rules` has checked it."""

    rules: list[PolicyRuleDraft] = Field(max_length=10)
    no_relevant_policy: bool = Field(description="True if no search result covered the case.")
    conflicting_passages: bool = Field(description="True if passages used contradict each other.")


class PolicyFindings(BaseModel):
    """The findings after the grounding check: every rule cites a passage that was found."""

    rules: list[PolicyRuleDraft]
    dropped_rules: list[PolicyRuleDraft]  # cited a chunk_key no search returned
    policy_versions: list[str]  # of the kept citations, e.g. "refund_policy v2"
    no_relevant_policy: bool
    conflicting_passages: bool
    ungrounded_citations: bool  # True: at least one rule was dropped


class PolicyFailure(StrEnum):
    """Why the agent stopped early. Values match WorkflowError codes."""

    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    OUTPUT_INVALID = "POLICY_OUTPUT_INVALID"
    STEP_LIMIT = "POLICY_STEP_LIMIT"


@dataclass(frozen=True)
class PolicySearch:
    """One search the agent made, for reviewers and scripts.

    Holds model-written text: keep it in memory, do not log it.
    """

    call_id: str
    query: str
    versions_considered: list[str]
    no_relevant_evidence: bool
    citations: list[str]  # the passages returned, best first


@dataclass(frozen=True)
class PolicyOutcome:
    trace: list[ToolCallRecord]  # content-free, safe for the audit trail
    searches: list[PolicySearch]  # successful searches only
    findings: PolicyFindings | None  # None on failure
    failure: PolicyFailure | None


@cache
def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def _search_outputs(results: list[ToolResult]) -> list[tuple[ToolResult, PolicySearchResult]]:
    return [
        (result, result.output)
        for result in results
        if result.tool == "search_policy" and isinstance(result.output, PolicySearchResult)
    ]


def _summarise(results: list[ToolResult]) -> list[PolicySearch]:
    return [
        PolicySearch(
            call_id=result.call_id,
            query=str(result.args.get("query", "")),
            versions_considered=output.versions_considered,
            no_relevant_evidence=output.no_relevant_evidence,
            citations=[passage.citation for passage in output.passages],
        )
        for result, output in _search_outputs(results)
    ]


def ground_rules(draft: PolicyFindingsDraft, results: list[ToolResult]) -> PolicyFindings:
    """Keep only rules whose citation is a passage this run's searches returned."""
    found: dict[str, PolicyPassage] = {
        passage.citation: passage
        for _, output in _search_outputs(results)
        for passage in output.passages
    }
    kept = [rule for rule in draft.rules if rule.citation in found]
    dropped = [rule for rule in draft.rules if rule.citation not in found]
    versions = sorted({f"{found[r.citation].policy_key} {found[r.citation].version}" for r in kept})
    return PolicyFindings(
        rules=kept,
        dropped_rules=dropped,
        policy_versions=versions,
        # Code, not the model, has the last word: nothing grounded means no policy found.
        no_relevant_policy=draft.no_relevant_policy or not kept,
        conflicting_passages=draft.conflicting_passages,
        ungrounded_citations=bool(dropped),
    )


def _case_message(
    complaint_text: str, category: str, order_number: str, focus: str | None
) -> ChatMessage:
    # The order number and category were checked by code before this agent runs. The
    # complaint and the supervisor's hint are untrusted text.
    text = (
        f"Order number for this case: {order_number}\nComplaint category: {category}\n\n"
        + wrap_untrusted("customer_complaint", complaint_text)
    )
    if focus:
        text += "\n\n" + wrap_untrusted("supervisor_hint", focus)
    return user_message(text)


async def research_policy(
    llm: LLMClient,
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    complaint_text: str,
    category: str,
    order_number: str,
    max_searches: int,
    search_tier: ModelTier,
    findings_tier: ModelTier,
    focus: str | None = None,
) -> PolicyOutcome:
    """Run search loop -> findings. `registry` is cut down to POLICY_TOOLS here."""
    loop = ToolLoop(
        llm, registry.restricted(POLICY_TOOLS), ctx, max_calls=max_searches, tier=search_tier
    )
    base = [
        system_message(load_prompt()),
        _case_message(complaint_text, category, order_number, focus),
    ]

    def outcome(findings: PolicyFindings | None, failure: PolicyFailure | None) -> PolicyOutcome:
        return PolicyOutcome(list(loop.trace), _summarise(loop.results), findings, failure)

    try:
        looped = await loop.run([*base, user_message(_SEARCH_INSTRUCTION)])
        if looped.step_limit_reached:
            return outcome(None, PolicyFailure.STEP_LIMIT)
        draft = await llm.structured(
            [*looped.messages, user_message(_FINDINGS_INSTRUCTION)],
            PolicyFindingsDraft,
            tier=findings_tier,
        )
    except LLMUnavailableError:
        return outcome(None, PolicyFailure.LLM_UNAVAILABLE)
    except (StructuredOutputError, ToolLoopOutputError):
        return outcome(None, PolicyFailure.OUTPUT_INVALID)
    return outcome(ground_rules(draft, loop.results), None)
