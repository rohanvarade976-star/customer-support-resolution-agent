"""The resolution agent: one structured call that drafts what to do and what to tell the customer.

It runs on the large model (PRD "Agent specifications", FR-9). Its inputs are the grounded
evidence, the grounded policy findings and the eligibility decision that *code* computed
(services/eligibility). The model has no tools.

The model only proposes. Code decides what counts (domain/validation.py, agents/resolve.py):
- `ResolutionDraft` has no amount field. For a refund, code fills the amount from the
  eligibility decision. If a model sends an `amount_minor` anyway it is ignored.
- Evidence, policy rules and the complaint are shown inside untrusted-data tags.
- On a revision the model sees the validator's reason codes (code-written) and the critic's
  issues (model-written, so wrapped as untrusted data).
"""

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from support_agent.agents.investigator import Evidence
from support_agent.agents.policy import PolicyFindings
from support_agent.domain.eligibility import EligibilityDecision
from support_agent.domain.enums import ResolutionAction
from support_agent.domain.validation import ValidationCode
from support_agent.llm.client import LLMClient
from support_agent.llm.tool_loop import wrap_untrusted
from support_agent.llm.types import ChatMessage, ModelTier, system_message, user_message

PROMPT_PATH = Path(__file__).parent / "prompts" / "resolution.md"
DRAFT_TIER: ModelTier = "large"

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=400)]

# What each validator code means, in words the drafting model can act on. Written by us,
# so they are safe to show as instructions (unlike the critic's text).
_CODE_HINTS: dict[ValidationCode, str] = {
    ValidationCode.REFUND_NOT_ELIGIBLE: "Not eligible per the decision: do not choose refund.",
    ValidationCode.REFUND_AMOUNT_MISMATCH: "A refund uses the eligible amount; the system sets it.",
    ValidationCode.AMOUNT_NOT_ALLOWED: "Only a refund may carry an amount.",
    ValidationCode.NO_EVIDENCE_CITED: "Cite the call_id of the facts you rely on.",
    ValidationCode.CITED_CALL_NOT_FOUND: "You cited a call_id that does not exist. Copy exactly.",
    ValidationCode.CITED_CALL_FAILED: "You cited a failed tool call. Cite successful calls only.",
    ValidationCode.NO_POLICY_CITED: "A refund must cite the policy rule it rests on.",
    ValidationCode.CITATION_NOT_GROUNDED: "You cited a policy citation not in the rules given.",
    ValidationCode.POLICY_NOT_FOUND: "No policy covers this case: do not choose refund.",
    ValidationCode.UNVERIFIED_CLAIM: (
        "The message says or implies a refund was issued, approved or completed, or says when "
        "money arrives. Nothing is done yet: say only that it is proposed and needs approval."
    ),
    ValidationCode.UNVERIFIED_AMOUNT: "Remove every money figure from the message.",
    ValidationCode.MESSAGE_EMPTY: "Write a customer message.",
    ValidationCode.REQUEST_INFO_NO_QUESTION: "When you request information, ask it as a question.",
}


class ResolutionDraft(BaseModel):
    """What the model must return. Not trusted until `validate_resolution` has passed it."""

    # Deliberately no amount field. An extra `amount_minor` from the model is ignored.
    model_config = ConfigDict(extra="ignore")

    action: ResolutionAction
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=800)] = Field(
        description="Two or three sentences for the reviewer: what the evidence shows and why."
    )
    cited_call_ids: list[Annotated[str, StringConstraints(max_length=100)]] = Field(
        max_length=10, description="call_id of each fact relied on, copied exactly."
    )
    policy_citations: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        max_length=10, description="Citation of each policy rule relied on, copied exactly."
    )
    customer_message: Annotated[str, StringConstraints(min_length=1, max_length=1500)] = Field(
        description="The reply to the customer. No money figures, no completed-refund claims."
    )


@dataclass(frozen=True)
class ResolutionInputs:
    """Everything the resolution and critic agents see about the case. Built by code."""

    complaint_text: str  # untrusted
    order_number: str  # verified by code before the investigator ran
    evidence: Evidence
    findings: PolicyFindings
    eligibility: EligibilityDecision


@cache
def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def render_case(inputs: ResolutionInputs) -> str:
    """The case as text: untrusted blocks for data, plain lines for what code decided."""
    facts = "\n".join(f"[{fact.call_id}] {fact.statement}" for fact in inputs.evidence.facts)
    missing = "\n".join(f"- {item}" for item in inputs.evidence.missing_information)
    rules = "\n".join(f"[{rule.citation}] {rule.statement}" for rule in inputs.findings.rules)
    decision = inputs.eligibility
    reasons = ", ".join(reason.value for reason in decision.reasons) or "none"
    return "\n\n".join(
        [
            f"Order number for this case: {inputs.order_number}",
            wrap_untrusted("customer_complaint", inputs.complaint_text),
            wrap_untrusted("evidence", facts or "(no verified facts)"),
            wrap_untrusted("missing_information", missing or "(none)"),
            wrap_untrusted("policy_rules", rules or "(no applicable rules found)"),
            "Eligibility decision, computed by the system from verified records (final):\n"
            f"eligible: {'yes' if decision.eligible else 'no'}\n"
            f"reasons it is blocked: {reasons}\n"
            "If eligible, the system sets the refund amount. You do not write it.",
        ]
    )


def initial_messages(inputs: ResolutionInputs) -> list[ChatMessage]:
    return [
        system_message(load_prompt()),
        user_message(render_case(inputs) + "\n\nDraft the resolution now."),
    ]


def revision_messages(
    inputs: ResolutionInputs,
    previous: ResolutionDraft,
    codes: list[ValidationCode],
    critic_issues: list[str],
) -> list[ChatMessage]:
    """The case, the rejected draft, and why it was rejected."""
    sections = [f"- {code.value}: {_CODE_HINTS.get(code, 'Fix this.')}" for code in codes]
    request = "Your draft was not accepted. Write a corrected draft."
    if sections:
        request += "\n\nChecks that failed:\n" + "\n".join(sections)
    if critic_issues:
        request += "\n\n" + wrap_untrusted(
            "reviewer_comments", "\n".join(f"- {issue}" for issue in critic_issues)
        )
    return [
        system_message(load_prompt()),
        user_message(render_case(inputs) + "\n\nDraft the resolution now."),
        ChatMessage(role="assistant", content=previous.model_dump_json()),
        user_message(request),
    ]


async def draft_resolution(llm: LLMClient, messages: list[ChatMessage]) -> ResolutionDraft:
    """One structured call on the large model. May raise LLM errors (resolve.py handles them)."""
    return await llm.structured(messages, ResolutionDraft, tier=DRAFT_TIER)
