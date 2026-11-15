"""Is an approval ready to approve, or does it need a reviewer's attention? (ADR-022)

A pure function over facts that code already computed: the approval's state, the eligibility
rules applied now, the risk flags and the quality checks read from the audit trail. No model
is asked and no model text is read. The case page and every queue row call this same function,
so they can never disagree.

"Ready to approve" needs every one of these:
- the approval is pending and not expired;
- the order can be read, is still eligible now, and the rules still give the stored amount;
- no risk flags;
- the workflow proposed it (a reviewer's manual proposal had no AI checks to pass);
- the AI recommended a refund, its draft was accepted and passed the validator;
- the critic approved;
- no policy citation was dropped by code;
- the approval does not expire within EXPIRING_SOON.

Otherwise "needs attention", with one reason per failed rule, most serious first. A reason
carries codes only (for example the risk flag codes or the eligibility reason codes).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from support_agent.domain.enums import ApprovalDecision, ResolutionAction

# The same cut-off the reviewer's screen already uses for "urgent" (frontend lib/time.ts).
EXPIRING_SOON = timedelta(minutes=10)


class Readiness(StrEnum):
    READY_TO_APPROVE = "ready_to_approve"
    NEEDS_ATTENTION = "needs_attention"


class ReadinessCode(StrEnum):
    """Why a case needs attention. The order here is the order of seriousness."""

    APPROVAL_NOT_PENDING = "APPROVAL_NOT_PENDING"  # already approved, rejected or expired
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"  # pending, but past its expiry time
    ORDER_UNAVAILABLE = "ORDER_UNAVAILABLE"  # the order's facts could not be read now
    NOT_ELIGIBLE_NOW = "NOT_ELIGIBLE_NOW"  # a check that passed then fails now
    AMOUNT_CHANGED = "AMOUNT_CHANGED"  # the rules now give a different refundable amount
    RISK_FLAG = "RISK_FLAG"  # one or more code-computed risk flags (ADR-021)
    AI_DID_NOT_RECOMMEND_REFUND = "AI_DID_NOT_RECOMMEND_REFUND"
    VALIDATOR_NOT_PASSED = "VALIDATOR_NOT_PASSED"  # no accepted draft passed the validator
    CRITIC_NOT_APPROVED = "CRITIC_NOT_APPROVED"  # the critic asked for changes or did not run
    CITATIONS_DROPPED = "CITATIONS_DROPPED"  # code dropped policy citations it could not verify
    REVIEWER_PROPOSED = "REVIEWER_PROPOSED"  # a manual proposal: no AI checks were run
    EXPIRING_SOON = "EXPIRING_SOON"  # less than EXPIRING_SOON left to decide


@dataclass(frozen=True)
class ReadinessReason:
    code: ReadinessCode
    codes: list[str] = field(default_factory=list)  # e.g. risk flag or eligibility codes


@dataclass(frozen=True)
class ReadinessInput:
    """Everything the verdict depends on, already computed by code."""

    approval_status: ApprovalDecision
    expires_at: datetime
    proposal_amount_minor: int
    proposed_by_reviewer: bool  # a manual proposal (action_proposals.proposed_by is set)
    # The eligibility rules applied now; all None when the order could not be read.
    order_readable: bool
    eligible_now: bool | None
    reasons_now: list[str]
    refundable_now_minor: int | None
    # From the run's audit trail (domain/run_analysis.py).
    risk_flags: list[str]
    ai_action: str | None
    draft_accepted: bool
    validator_passed: bool | None
    validation_codes: list[str]
    critic_verdict: str | None
    citations_dropped: int


@dataclass(frozen=True)
class ReadinessResult:
    status: Readiness
    reasons: list[ReadinessReason]


def _approval_reasons(data: ReadinessInput, now: datetime) -> list[ReadinessReason]:
    if data.approval_status != ApprovalDecision.PENDING:
        return [ReadinessReason(ReadinessCode.APPROVAL_NOT_PENDING, [data.approval_status.value])]
    if now >= data.expires_at:
        return [ReadinessReason(ReadinessCode.APPROVAL_EXPIRED)]
    return []


def _order_reasons(data: ReadinessInput) -> list[ReadinessReason]:
    if not data.order_readable:
        return [ReadinessReason(ReadinessCode.ORDER_UNAVAILABLE)]
    if data.eligible_now is not True:
        return [ReadinessReason(ReadinessCode.NOT_ELIGIBLE_NOW, list(data.reasons_now))]
    if data.refundable_now_minor != data.proposal_amount_minor:
        return [ReadinessReason(ReadinessCode.AMOUNT_CHANGED)]
    return []


def _ai_reasons(data: ReadinessInput) -> list[ReadinessReason]:
    """The AI's checks. A manual proposal had none, so it gets one honest reason instead."""
    if data.proposed_by_reviewer:
        return [ReadinessReason(ReadinessCode.REVIEWER_PROPOSED)]
    reasons: list[ReadinessReason] = []
    if data.ai_action != ResolutionAction.REFUND:
        action = [] if data.ai_action is None else [data.ai_action]
        reasons.append(ReadinessReason(ReadinessCode.AI_DID_NOT_RECOMMEND_REFUND, action))
    if not data.draft_accepted or data.validator_passed is not True:
        reasons.append(
            ReadinessReason(ReadinessCode.VALIDATOR_NOT_PASSED, list(data.validation_codes))
        )
    if data.critic_verdict != "approve":
        verdict = [data.critic_verdict or "not_run"]
        reasons.append(ReadinessReason(ReadinessCode.CRITIC_NOT_APPROVED, verdict))
    if data.citations_dropped > 0:
        reasons.append(
            ReadinessReason(ReadinessCode.CITATIONS_DROPPED, [str(data.citations_dropped)])
        )
    return reasons


def assess(data: ReadinessInput, now: datetime) -> ReadinessResult:
    """The verdict and its reasons, most serious first."""
    reasons = [*_approval_reasons(data, now), *_order_reasons(data)]
    if data.risk_flags:
        reasons.append(ReadinessReason(ReadinessCode.RISK_FLAG, list(data.risk_flags)))
    reasons.extend(_ai_reasons(data))
    pending = data.approval_status == ApprovalDecision.PENDING
    if pending and now < data.expires_at <= now + EXPIRING_SOON:
        reasons.append(ReadinessReason(ReadinessCode.EXPIRING_SOON))
    status = Readiness.NEEDS_ATTENTION if reasons else Readiness.READY_TO_APPROVE
    return ReadinessResult(status, reasons)
