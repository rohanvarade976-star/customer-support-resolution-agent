"""Deterministic checks on a drafted resolution. Pure: no database, no clock, no LLM.

The resolution agent only proposes. This module decides whether a proposal may stand
(PRD FR-11, "Hard business rules in code"). It returns reason codes; an empty list means
the draft passed. It checks:

- a refund only when code found the order eligible, and for exactly the eligible amount;
- every cited tool call exists and succeeded;
- every policy citation is one the policy agent's grounded findings contain;
- the structured tool outputs agree with each other (order vs carrier delivery time);
- the customer message does not claim an outcome nobody has verified, and does not promise an
  investigation, compensation or any other action that code-computed facts do not support.

The message check is a best-effort keyword filter, not a proof: it catches the common ways
of saying "the refund is done". The real protection is that no agent can move money, and
that the final customer message is built from verified state (later milestones).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from support_agent.domain.eligibility import EligibilityDecision
from support_agent.domain.enums import ResolutionAction
from support_agent.domain.shipping import ShippingDelayFacts

OUTCOME_OK = "ok"  # a succeeded call's outcome in the tool trace (same value as the tool loop)

MINOR_UNITS_PER_MAJOR = 100  # paise per rupee


class ValidationCode(StrEnum):
    REFUND_NOT_ELIGIBLE = "REFUND_NOT_ELIGIBLE"
    REFUND_AMOUNT_MISMATCH = "REFUND_AMOUNT_MISMATCH"
    AMOUNT_NOT_ALLOWED = "AMOUNT_NOT_ALLOWED"  # a non-refund action carries an amount
    NO_EVIDENCE_CITED = "NO_EVIDENCE_CITED"
    CITED_CALL_NOT_FOUND = "CITED_CALL_NOT_FOUND"
    CITED_CALL_FAILED = "CITED_CALL_FAILED"
    NO_POLICY_CITED = "NO_POLICY_CITED"
    CITATION_NOT_GROUNDED = "CITATION_NOT_GROUNDED"
    POLICY_NOT_FOUND = "POLICY_NOT_FOUND"
    POLICY_CONFLICT = "POLICY_CONFLICT"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    UNVERIFIED_CLAIM = "UNVERIFIED_CLAIM"
    UNVERIFIED_AMOUNT = "UNVERIFIED_AMOUNT"
    UNSUPPORTED_PROMISE = "UNSUPPORTED_PROMISE"  # promises an action no verified fact supports
    MESSAGE_EMPTY = "MESSAGE_EMPTY"
    REQUEST_INFO_NO_QUESTION = "REQUEST_INFO_NO_QUESTION"


# Problems a better draft cannot fix: the data or the policy itself is inconsistent.
# The case goes straight to a human, without spending revisions.
ESCALATE_IMMEDIATELY = frozenset({ValidationCode.EVIDENCE_CONFLICT, ValidationCode.POLICY_CONFLICT})


@dataclass(frozen=True)
class DraftClaim:
    """What a draft asserts. `amount_minor` is filled by code, never taken from the model."""

    action: ResolutionAction
    amount_minor: int | None
    cited_call_ids: tuple[str, ...]
    policy_citations: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class DeliveryDates:
    """Delivery time from the order record and from the carrier. None = not delivered yet."""

    order_delivered_at: datetime | None
    shipment_delivered_at: datetime | None


@dataclass(frozen=True)
class ValidationContext:
    """The verified facts the claim is checked against. All built by code."""

    eligibility: EligibilityDecision
    call_outcomes: Mapping[str, str]  # call_id -> OUTCOME_OK or the error code
    grounded_citations: frozenset[str]  # citations of the policy agent's grounded rules
    policy_conflict: bool  # the policy agent saw contradicting passages
    no_relevant_policy: bool
    # None when the order or the shipment was not looked up successfully: nothing to compare.
    delivery_dates: DeliveryDates | None
    delivery_tolerance: timedelta
    # Code-computed delay facts; None when there is no shipment to compute them from.
    delay_facts: ShippingDelayFacts | None = None


def normalize_citation(citation: str) -> str:
    """Strip the wrapping a model adds around a citation: whitespace, brackets, quotes.

    "[refund_policy_v2:section_3]" and ' "refund_policy_v2:section_3" ' both become
    "refund_policy_v2:section_3". Only the ends are touched, so a made-up citation stays
    made-up and still fails the grounding check.
    """
    return citation.strip(" \t\r\n[](){}<>'\"`")


def delivery_dates_conflict(dates: DeliveryDates, tolerance: timedelta) -> bool:
    """True if the order record and the carrier disagree about delivery.

    Disagreement is: only one of them says delivered, or both do but the times differ by
    more than `tolerance`. Eligibility is computed from the order's date, so a carrier that
    says otherwise means the refund window may be wrong.
    """
    order, carrier = dates.order_delivered_at, dates.shipment_delivered_at
    if order is None and carrier is None:
        return False
    if order is None or carrier is None:
        return True
    return abs(order - carrier) > tolerance


# --- the message check ---------------------------------------------------------------------

_REFUND_WORD = r"(?:refund(?:ed|s)?|money\s+back|reimburse(?:d|ment)?|credit(?:ed)?)"
_DONE_WORD = r"(?:issued|processed|completed|sent|credited|initiated|approved|made|paid|returned)"
_OVERCLAIM_PATTERNS = [
    # "has been refunded", "have already been refunded", "was refunded"
    re.compile(r"\b(?:has|have|had|was|were)\s+(?:already\s+|now\s+|been\s+)*refunded\b"),
    # "we've refunded", "I refunded", "we have issued your refund"
    re.compile(r"\b(?:we|i)(?:'ve|\s+have|\s+had)?\s+(?:already\s+|now\s+)?refunded\b"),
    re.compile(
        r"\b(?:we|i)(?:'ve|\s+have|\s+had)?\s+(?:already\s+|now\s+)?"
        rf"(?:issued|processed|credited|initiated|approved|sent)\s+(?:you\s+)?(?:your|the|a)\s+"
        rf"{_REFUND_WORD}\b"
    ),
    # "your refund has been issued", "the refund is complete(d)", "refund was approved"
    re.compile(
        rf"\b{_REFUND_WORD}\b.{{0,40}}\b(?:has|have|had|is|are|was|were)\s+"
        rf"(?:already\s+|now\s+|been\s+|successfully\s+)*{_DONE_WORD}\b"
    ),
    re.compile(rf"\b{_REFUND_WORD}\b.{{0,30}}\b(?:is|are)\s+(?:complete|done|on\s+its\s+way)\b"),
    # timing nobody has verified: "the refund will arrive within 5-7 business days". A policy
    # window such as "returns within 30 days" is not about money arriving, so it is allowed.
    re.compile(
        rf"\b{_REFUND_WORD}\b.{{0,60}}\b(?:within|in|after)\s+\d+"
        r"(?:\s*[^\d\s]{1,3}\s*\d+)?"
        r"\s+(?:business\s+|working\s+)?days\b"
    ),
]

_MONEY_FIGURE = re.compile(r"(?:₹|\brs\.?|\binr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)


# --- promises and delay claims -------------------------------------------------------------
# Checked one sentence at a time. A negated sentence ("we cannot offer compensation") is
# information, not a promise, so it is skipped.

_NEGATION = re.compile(r"\b(?:not|no|never|cannot|unable)\b|n't")
_INVESTIGATION = re.compile(
    r"\binvestigat(?:e|es|ed|ing|ion)\b|\b(?:open|start|launch|begin|begun|opened|started|launched)\w*"
    r"\s+(?:a|an|the)\s+(?:\w+\s+)?(?:inquiry|enquiry|trace|search)\b"
)
# Money or goods the policy does not let support agents offer (shipping policy, section 4).
_COMPENSATION = re.compile(
    r"\b(?:compensat\w+|vouchers?|coupons?|goodwill|store\s+credit|partial\s+refund|"
    r"discount|replacement|replace)\b"
)
_SPECIALIST = re.compile(
    r"\b(?:specialists?|escalat\w+|supervisor|senior\s+\w+|"
    r"member\s+of\s+(?:our\s+)?(?:support\s+)?team|colleague)\b"
)
# "will look into it", "have passed it on", "is being reviewed": the sentence commits us to act.
_COMMITMENT = re.compile(
    r"\b(?:will|shall|going\s+to|have|has|had|been|being|are\s+(?:passing|forwarding|sending|"
    r"referring)|passed|forwarded|referred|looking\s+into)\b|'ll\b|'ve\b"
)
_DELAY_CLAIM = re.compile(
    r"\b(?:(over|more\s+than|at\s+least|exceeding)\s+)?(\d+)\s+(?:calendar\s+|business\s+)?"
    r"days?\s+(?:past|late|overdue|behind)\b"
)


def _sentences(message: str) -> list[str]:
    text = " ".join(message.lower().split())
    return [part for part in re.split(r"(?<=[.!?])\s+", text) if part]


def promises_unsupported_action(
    message: str, action: ResolutionAction, facts: ShippingDelayFacts | None
) -> bool:
    """True if the message promises an action that nothing in code's facts supports.

    Allowed: an investigation or a specialist when the policy threshold applies (`facts`);
    escalation or specialist wording when the action itself is escalate. Never allowed:
    compensation, vouchers, replacements. Telling the customer they may *request* a review
    is information (no commitment verb), so it is not flagged.
    """
    investigation_ok = facts is not None and facts.investigation_applies
    specialist_ok = investigation_ok or action == ResolutionAction.ESCALATE
    for sentence in _sentences(message):
        if _NEGATION.search(sentence):
            continue
        if _COMPENSATION.search(sentence) and _COMMITMENT.search(sentence):
            return True
        if _INVESTIGATION.search(sentence) and not investigation_ok:
            return True
        if _SPECIALIST.search(sentence) and _COMMITMENT.search(sentence) and not specialist_ok:
            return True
    return False


def states_unverified_delay(message: str, facts: ShippingDelayFacts | None) -> bool:
    """True if the message gives a number of days late that code's facts do not support.

    "N days late" must equal the computed days. "over N days late" (or "more than", "at
    least") is fine only if N is below the computed days.
    """
    text = " ".join(message.lower().split())
    for qualifier, number in _DELAY_CLAIM.findall(text):
        days = int(number)
        if facts is None:
            return True
        if days != facts.days_past_expected and not (qualifier and days < facts.days_past_expected):
            return True
    return False


def claims_unverified_outcome(message: str) -> bool:
    text = " ".join(message.lower().split())
    return any(pattern.search(text) for pattern in _OVERCLAIM_PATTERNS)


def _figures_minor(message: str) -> list[int]:
    """Every rupee amount written in the message, in paise (Decimal, never float)."""
    return [
        int(Decimal(match.replace(",", "")) * MINOR_UNITS_PER_MAJOR)
        for match in _MONEY_FIGURE.findall(message)
    ]


# --- the checks ----------------------------------------------------------------------------


def _check_action(claim: DraftClaim, ctx: ValidationContext) -> list[ValidationCode]:
    decision = ctx.eligibility
    if claim.action == ResolutionAction.REFUND:
        codes: list[ValidationCode] = []
        if not decision.eligible:
            codes.append(ValidationCode.REFUND_NOT_ELIGIBLE)
        if claim.amount_minor != decision.refundable_amount_minor or not claim.amount_minor:
            codes.append(ValidationCode.REFUND_AMOUNT_MISMATCH)
        return codes
    codes = [] if claim.amount_minor is None else [ValidationCode.AMOUNT_NOT_ALLOWED]
    if claim.action == ResolutionAction.REQUEST_INFO and "?" not in claim.message:
        codes.append(ValidationCode.REQUEST_INFO_NO_QUESTION)
    return codes


def _check_cited_calls(claim: DraftClaim, ctx: ValidationContext) -> list[ValidationCode]:
    codes: list[ValidationCode] = []
    needs_evidence = claim.action in (ResolutionAction.REFUND, ResolutionAction.EXPLAIN_STATUS)
    if needs_evidence and not claim.cited_call_ids:
        codes.append(ValidationCode.NO_EVIDENCE_CITED)
    unknown = [cid for cid in claim.cited_call_ids if cid not in ctx.call_outcomes]
    if unknown:
        codes.append(ValidationCode.CITED_CALL_NOT_FOUND)
    if any(ctx.call_outcomes.get(cid, OUTCOME_OK) != OUTCOME_OK for cid in claim.cited_call_ids):
        codes.append(ValidationCode.CITED_CALL_FAILED)
    return codes


def _check_policy(claim: DraftClaim, ctx: ValidationContext) -> list[ValidationCode]:
    codes: list[ValidationCode] = []
    if any(c not in ctx.grounded_citations for c in claim.policy_citations):
        codes.append(ValidationCode.CITATION_NOT_GROUNDED)
    if claim.action == ResolutionAction.REFUND:
        if ctx.no_relevant_policy:
            codes.append(ValidationCode.POLICY_NOT_FOUND)
        elif not claim.policy_citations:
            codes.append(ValidationCode.NO_POLICY_CITED)
    return codes


def _check_conflicts(ctx: ValidationContext) -> list[ValidationCode]:
    codes: list[ValidationCode] = []
    if ctx.policy_conflict:
        codes.append(ValidationCode.POLICY_CONFLICT)
    dates = ctx.delivery_dates
    if dates is not None and delivery_dates_conflict(dates, ctx.delivery_tolerance):
        codes.append(ValidationCode.EVIDENCE_CONFLICT)
    return codes


def _check_message(claim: DraftClaim, ctx: ValidationContext) -> list[ValidationCode]:
    if not claim.message.strip():
        return [ValidationCode.MESSAGE_EMPTY]
    codes: list[ValidationCode] = []
    if claims_unverified_outcome(claim.message):
        codes.append(ValidationCode.UNVERIFIED_CLAIM)
    if states_unverified_delay(claim.message, ctx.delay_facts):
        codes.append(ValidationCode.UNVERIFIED_CLAIM)
    if promises_unsupported_action(claim.message, claim.action, ctx.delay_facts):
        codes.append(ValidationCode.UNSUPPORTED_PROMISE)
    # A rupee figure may only be the eligible refund amount, and only in a refund draft.
    is_refund = claim.action == ResolutionAction.REFUND
    allowed = {ctx.eligibility.refundable_amount_minor} if is_refund else set()
    if any(figure not in allowed for figure in _figures_minor(claim.message)):
        codes.append(ValidationCode.UNVERIFIED_AMOUNT)
    return codes


def validate_resolution(claim: DraftClaim, ctx: ValidationContext) -> list[ValidationCode]:
    """Every reason the draft may not stand. Empty means it passed. Order is stable."""
    return [
        *_check_action(claim, ctx),
        *_check_cited_calls(claim, ctx),
        *_check_policy(claim, ctx),
        *_check_conflicts(ctx),
        *_check_message(claim, ctx),
    ]
