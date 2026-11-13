"""Draft -> validate -> critique -> revise, then a final validation. Code has the last word.

    draft (resolution agent)
      -> validate (domain/validation.py)       fails -> revise, or escalate
      -> critic                                 revise -> revise, or escalate
      -> final validation                       fails -> escalate
      -> accepted outcome

- The validator always runs before the critic, so the critic only reads drafts that already
  passed the hard checks. The critic's verdict can only add objections, never remove one:
  an approval does not matter to a draft the validator rejected.
- At most `max_revisions` revisions (PRD FR-10: one by default). When they run out, or the
  data itself is inconsistent (an evidence or policy conflict), the outcome is `escalate`
  with reason codes. Nothing else produces an accepted outcome: every accepted draft has
  just passed `validate_resolution`.
- The refund amount in the outcome is the eligibility decision's, filled in by code.
- An escalation uses a fixed message written here, not the model's draft, because the draft
  was rejected.

LLM failures do not raise: they come back as an escalation with `failure` set, like the
other agents. Reason codes and logs hold codes only, never model text.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from support_agent.agents.critic import CriticVerdict, review_draft
from support_agent.agents.resolution import (
    ResolutionDraft,
    ResolutionInputs,
    draft_resolution,
    initial_messages,
    revision_messages,
)
from support_agent.domain.enums import ResolutionAction
from support_agent.domain.validation import (
    ESCALATE_IMMEDIATELY,
    DeliveryDates,
    DraftClaim,
    ValidationCode,
    ValidationContext,
    normalize_citation,
    validate_resolution,
)
from support_agent.integrations.shipping import Shipment
from support_agent.llm.client import LLMClient
from support_agent.llm.errors import LLMUnavailableError, StructuredOutputError
from support_agent.llm.tool_loop import ToolCallRecord, ToolResult
from support_agent.llm.types import ModelTier
from support_agent.schemas.orders import OrderResponse

log = logging.getLogger(__name__)

# Shown to the customer when the case goes to a person. It promises nothing about the result.
ESCALATION_MESSAGE = (
    "Your case needs a closer look from a member of our support team. "
    "We are passing it to them and they will follow up with you."
)


class EscalationReason(StrEnum):
    """Why a case was escalated, besides the validator's own codes."""

    MODEL_ESCALATED = "MODEL_ESCALATED"  # the draft itself chose escalate and passed validation
    CRITIC_OBJECTED = "CRITIC_OBJECTED"
    REVISIONS_EXHAUSTED = "REVISIONS_EXHAUSTED"


class ResolutionFailure(StrEnum):
    """An LLM problem that stopped the loop. Values match WorkflowError codes."""

    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    RESOLUTION_OUTPUT_INVALID = "RESOLUTION_OUTPUT_INVALID"
    CRITIC_OUTPUT_INVALID = "CRITIC_OUTPUT_INVALID"


@dataclass(frozen=True)
class Attempt:
    """One draft, what the validator said, and what the critic said (None: it did not run).

    Holds model-written text: keep it in memory, do not log it.
    """

    draft: ResolutionDraft
    validation: list[ValidationCode]
    verdict: CriticVerdict | None


@dataclass(frozen=True)
class ResolutionOutcome:
    action: ResolutionAction
    amount_minor: int | None  # only for refund; always the eligibility decision's amount
    message: str  # the draft's, or ESCALATION_MESSAGE when a rejected draft was replaced
    draft: ResolutionDraft | None  # the accepted draft (rationale and citations); None if replaced
    reason_codes: list[str]  # empty for an accepted non-escalation
    attempts: list[Attempt] = field(default_factory=list)
    failure: ResolutionFailure | None = None


def _delivery_dates(results: list[ToolResult]) -> DeliveryDates | None:
    """Delivery times from get_order and get_shipment, or None if either is missing."""
    orders = [r.output for r in results if isinstance(r.output, OrderResponse)]
    shipments = [r.output for r in results if isinstance(r.output, Shipment)]
    if not orders or not shipments:
        return None
    return DeliveryDates(orders[-1].delivered_at, shipments[-1].delivered_at)


def build_context(
    inputs: ResolutionInputs,
    trace: list[ToolCallRecord],
    results: list[ToolResult],
    delivery_tolerance: timedelta,
) -> ValidationContext:
    """Turn what the earlier agents found into the facts the validator checks against."""
    return ValidationContext(
        eligibility=inputs.eligibility,
        call_outcomes={record.call_id: record.outcome for record in trace},
        grounded_citations=frozenset(rule.citation for rule in inputs.findings.rules),
        policy_conflict=inputs.findings.conflicting_passages,
        no_relevant_policy=inputs.findings.no_relevant_policy,
        delivery_dates=_delivery_dates(results),
        delivery_tolerance=delivery_tolerance,
        delay_facts=inputs.delay_facts,
    )


def normalize_draft(draft: ResolutionDraft) -> ResolutionDraft:
    """Strip brackets, quotes and spaces a model wraps around citations and call ids.

    Done before any validation, so formatting alone never fails a draft. The values must
    still match a grounded citation or a real call exactly; nothing is guessed.
    """
    return draft.model_copy(
        update={
            "policy_citations": [normalize_citation(c) for c in draft.policy_citations],
            "cited_call_ids": [normalize_citation(c) for c in draft.cited_call_ids],
        }
    )


def to_claim(draft: ResolutionDraft, inputs: ResolutionInputs) -> DraftClaim:
    """The amount comes from the eligibility decision, never from the model."""
    is_refund = draft.action == ResolutionAction.REFUND
    return DraftClaim(
        action=draft.action,
        amount_minor=inputs.eligibility.refundable_amount_minor if is_refund else None,
        cited_call_ids=tuple(draft.cited_call_ids),
        policy_citations=tuple(draft.policy_citations),
        message=draft.customer_message,
    )


def escalation_outcome(
    reasons: list[str], attempts: list[Attempt], failure: ResolutionFailure | None = None
) -> ResolutionOutcome:
    log.info(
        "resolution_escalated",
        extra={"reason_codes": reasons, "attempts": len(attempts)},
    )
    return ResolutionOutcome(
        action=ResolutionAction.ESCALATE,
        amount_minor=None,
        message=ESCALATION_MESSAGE,
        draft=None,
        reason_codes=reasons,
        attempts=attempts,
        failure=failure,
    )


def final_outcome(
    draft: ResolutionDraft,
    inputs: ResolutionInputs,
    ctx: ValidationContext,
    attempts: list[Attempt],
) -> ResolutionOutcome:
    """The final validation. The only place an accepted outcome is made."""
    claim = to_claim(draft, inputs)
    failed = validate_resolution(claim, ctx)
    if failed:
        return escalation_outcome([code.value for code in failed], attempts)
    escalating = draft.action == ResolutionAction.ESCALATE
    log.info("resolution_accepted", extra={"action": draft.action.value, "attempts": len(attempts)})
    return ResolutionOutcome(
        action=draft.action,
        amount_minor=claim.amount_minor,
        message=draft.customer_message,
        draft=draft,
        reason_codes=[EscalationReason.MODEL_ESCALATED.value] if escalating else [],
        attempts=attempts,
    )


async def resolve(
    llm: LLMClient,
    inputs: ResolutionInputs,
    *,
    trace: list[ToolCallRecord],
    results: list[ToolResult],
    max_revisions: int,
    delivery_tolerance: timedelta,
    critic_tier: ModelTier,
) -> ResolutionOutcome:
    """Run the loop. `trace` and `results` are the investigator's (InvestigationOutcome)."""
    ctx = build_context(inputs, trace, results, delivery_tolerance)
    attempts: list[Attempt] = []
    messages = initial_messages(inputs)
    failure = ResolutionFailure.RESOLUTION_OUTPUT_INVALID  # which step a bad output came from
    revisions = 0
    try:
        while True:
            failure = ResolutionFailure.RESOLUTION_OUTPUT_INVALID
            draft = normalize_draft(await draft_resolution(llm, messages))
            codes = validate_resolution(to_claim(draft, inputs), ctx)

            if ESCALATE_IMMEDIATELY.intersection(codes):
                # The data disagrees with itself: no rewording fixes that. A person decides.
                attempts.append(Attempt(draft, codes, None))
                return escalation_outcome([code.value for code in codes], attempts)

            verdict: CriticVerdict | None = None
            if not codes and draft.action != ResolutionAction.ESCALATE:
                failure = ResolutionFailure.CRITIC_OUTPUT_INVALID
                verdict = await review_draft(llm, inputs, draft, tier=critic_tier)
            attempts.append(Attempt(draft, codes, verdict))

            objected = verdict is not None and verdict.verdict == "revise"
            if not codes and not objected:
                return final_outcome(draft, inputs, ctx, attempts)

            if revisions >= max_revisions:
                reasons = [code.value for code in codes]
                if objected:
                    reasons.append(EscalationReason.CRITIC_OBJECTED.value)
                reasons.append(EscalationReason.REVISIONS_EXHAUSTED.value)
                return escalation_outcome(reasons, attempts)

            revisions += 1
            issues = verdict.issues if verdict is not None else []
            messages = revision_messages(inputs, draft, codes, issues)
    except LLMUnavailableError:
        unavailable = ResolutionFailure.LLM_UNAVAILABLE
        return escalation_outcome([unavailable.value], attempts, failure=unavailable)
    except StructuredOutputError:
        return escalation_outcome([failure.value], attempts, failure=failure)
