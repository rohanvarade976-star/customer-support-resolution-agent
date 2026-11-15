"""The readiness verdict (domain/readiness.py, ADR-022): pure rules, no model.

One clean case is "ready to approve"; then each rule is broken on its own and must give
"needs attention" with exactly that reason. Also: reasons are ordered most serious first, and
a manual proposal is never ready (it had no AI checks).
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from support_agent.domain.enums import ApprovalDecision
from support_agent.domain.readiness import (
    EXPIRING_SOON,
    Readiness,
    ReadinessCode,
    ReadinessInput,
    ReadinessReason,
    assess,
)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

CLEAN = ReadinessInput(
    approval_status=ApprovalDecision.PENDING,
    expires_at=NOW + timedelta(minutes=45),
    proposal_amount_minor=249900,
    proposed_by_reviewer=False,
    order_readable=True,
    eligible_now=True,
    reasons_now=[],
    refundable_now_minor=249900,
    risk_flags=[],
    ai_action="refund",
    draft_accepted=True,
    validator_passed=True,
    validation_codes=[],
    critic_verdict="approve",
    citations_dropped=0,
)


def test_a_clean_case_is_ready_to_approve() -> None:
    result = assess(CLEAN, NOW)
    assert result.status == Readiness.READY_TO_APPROVE
    assert result.reasons == []


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {"approval_status": ApprovalDecision.APPROVED},
            ReadinessReason(ReadinessCode.APPROVAL_NOT_PENDING, ["approved"]),
        ),
        ({"expires_at": NOW}, ReadinessReason(ReadinessCode.APPROVAL_EXPIRED)),
        (
            {"order_readable": False, "eligible_now": None, "refundable_now_minor": None},
            ReadinessReason(ReadinessCode.ORDER_UNAVAILABLE),
        ),
        (
            {"eligible_now": False, "reasons_now": ["OUTSIDE_WINDOW"], "refundable_now_minor": 0},
            ReadinessReason(ReadinessCode.NOT_ELIGIBLE_NOW, ["OUTSIDE_WINDOW"]),
        ),
        ({"refundable_now_minor": 100000}, ReadinessReason(ReadinessCode.AMOUNT_CHANGED)),
        (
            {"risk_flags": ["INJECTION_SUSPECTED"]},
            ReadinessReason(ReadinessCode.RISK_FLAG, ["INJECTION_SUSPECTED"]),
        ),
        (
            {"ai_action": "explain_status"},
            ReadinessReason(ReadinessCode.AI_DID_NOT_RECOMMEND_REFUND, ["explain_status"]),
        ),
        (
            {"validator_passed": False, "validation_codes": ["UNGROUNDED_CLAIM"]},
            ReadinessReason(ReadinessCode.VALIDATOR_NOT_PASSED, ["UNGROUNDED_CLAIM"]),
        ),
        ({"draft_accepted": False}, ReadinessReason(ReadinessCode.VALIDATOR_NOT_PASSED)),
        (
            {"critic_verdict": "revise"},
            ReadinessReason(ReadinessCode.CRITIC_NOT_APPROVED, ["revise"]),
        ),
        (
            {"critic_verdict": None},
            ReadinessReason(ReadinessCode.CRITIC_NOT_APPROVED, ["not_run"]),
        ),
        ({"citations_dropped": 2}, ReadinessReason(ReadinessCode.CITATIONS_DROPPED, ["2"])),
        (
            {"expires_at": NOW + EXPIRING_SOON},
            ReadinessReason(ReadinessCode.EXPIRING_SOON),
        ),
    ],
)
def test_each_broken_rule_needs_attention_with_its_reason(
    changes: dict[str, object], expected: ReadinessReason
) -> None:
    result = assess(replace(CLEAN, **changes), NOW)  # type: ignore[arg-type]
    assert result.status == Readiness.NEEDS_ATTENTION
    assert result.reasons == [expected]


def test_a_manual_proposal_is_never_ready_and_skips_the_ai_checks() -> None:
    """A reviewer's proposal had no draft, validator or critic: one honest reason instead."""
    manual = replace(
        CLEAN,
        proposed_by_reviewer=True,
        ai_action="escalate",
        draft_accepted=False,
        validator_passed=None,
        critic_verdict=None,
    )
    result = assess(manual, NOW)
    assert result.status == Readiness.NEEDS_ATTENTION
    assert result.reasons == [ReadinessReason(ReadinessCode.REVIEWER_PROPOSED)]


def test_reasons_are_ordered_most_serious_first() -> None:
    broken = replace(
        CLEAN,
        expires_at=NOW + timedelta(minutes=5),
        critic_verdict="revise",
        risk_flags=["PROVIDER_FALLBACK"],
        eligible_now=False,
        reasons_now=["ALREADY_REFUNDED"],
    )
    codes = [reason.code for reason in assess(broken, NOW).reasons]
    assert codes == [
        ReadinessCode.NOT_ELIGIBLE_NOW,
        ReadinessCode.RISK_FLAG,
        ReadinessCode.CRITIC_NOT_APPROVED,
        ReadinessCode.EXPIRING_SOON,
    ]


def test_a_decided_approval_is_not_also_reported_as_expiring() -> None:
    decided = replace(
        CLEAN, approval_status=ApprovalDecision.REJECTED, expires_at=NOW + timedelta(minutes=1)
    )
    codes = [reason.code for reason in assess(decided, NOW).reasons]
    assert codes == [ReadinessCode.APPROVAL_NOT_PENDING]
