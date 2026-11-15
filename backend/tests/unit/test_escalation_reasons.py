"""Why a run was escalated, as codes from its audit events (domain/escalations.py, ADR-022)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from support_agent.domain.escalations import EscalationCode, escalation_reasons
from support_agent.domain.run_analysis import EventView

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
THRESHOLD = 0.7


def event(event_type: str, payload: dict[str, Any], i: int = 0) -> EventView:
    return EventView(event_type, "system", T0 + timedelta(seconds=i), payload)


CONFIDENT = event("run.classified", {"category": "refund", "confidence": 0.95})


def reasons(*events: EventView) -> list[tuple[EscalationCode, list[str]]]:
    return escalation_reasons([CONFIDENT, *events], THRESHOLD)


def test_the_ai_choosing_to_escalate() -> None:
    resolution = event("resolution.completed", {"reason_codes": ["MODEL_ESCALATED"]}, 1)
    assert reasons(resolution, event("run.escalated", {}, 2)) == [
        (EscalationCode.AI_CHOSE_TO_ESCALATE, [])
    ]


def test_drafts_rejected_by_code_keep_their_codes() -> None:
    resolution = event(
        "resolution.completed", {"reason_codes": ["CRITIC_OBJECTED", "UNGROUNDED_CLAIM"]}, 1
    )
    assert reasons(resolution) == [
        (EscalationCode.DRAFT_FAILED_CHECKS, ["CRITIC_OBJECTED", "UNGROUNDED_CLAIM"])
    ]


def test_an_llm_outage_is_reported_once_as_ai_unavailable() -> None:
    resolution = event(
        "resolution.completed",
        {"reason_codes": ["LLM_UNAVAILABLE"], "failure": "LLM_UNAVAILABLE"},
        1,
    )
    escalated = event("run.escalated", {"error_codes": ["LLM_UNAVAILABLE"]}, 2)
    assert reasons(resolution, escalated) == [(EscalationCode.AI_UNAVAILABLE, [])]


def test_refused_proposal_refund_and_dead_job() -> None:
    escalated = event(
        "run.escalated",
        {
            "error_codes": ["PROPOSAL_REFUSED", "CASE_FACTS_UNAVAILABLE"],
            "proposal_refusal": "NOT_ELIGIBLE",
        },
        1,
    )
    dead = event("run.escalated", {"reason": "JOB_DEAD", "job_type": "start_run"}, 2)
    assert reasons(escalated, dead) == [
        (EscalationCode.WORKFLOW_ERROR, ["CASE_FACTS_UNAVAILABLE"]),
        (EscalationCode.PROPOSAL_REFUSED, ["NOT_ELIGIBLE"]),
        (EscalationCode.JOB_FAILED, ["start_run"]),
    ]


def test_expired_approval_blocked_reply_and_risk_flags() -> None:
    found = escalation_reasons(
        [
            event("run.classified", {"category": "unclear", "confidence": 0.3}),
            event("approval.expired", {}, 1),
            event("reply.guardrail_replaced", {"codes": ["REPLY_INTERNAL_ID"]}, 2),
        ],
        THRESHOLD,
    )
    assert found == [
        (EscalationCode.APPROVAL_EXPIRED, []),
        (EscalationCode.REPLY_BLOCKED, ["REPLY_INTERNAL_ID"]),
        (EscalationCode.RISK_FLAG, ["LOW_CLASSIFIER_CONFIDENCE"]),
    ]


def test_no_recorded_reason_is_unknown() -> None:
    assert reasons(event("run.escalated", {}, 1)) == [(EscalationCode.UNKNOWN, [])]
