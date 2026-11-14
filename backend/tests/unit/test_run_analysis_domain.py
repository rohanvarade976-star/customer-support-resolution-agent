"""The reviewer's AI analysis, computed by code from audit events (domain/run_analysis.py).

Each risk flag is a fixed rule over the events: these tests build the events by hand and
check that each rule fires when it should and stays quiet otherwise. Also: agents in order,
code-made lookups, quality checks, cost, and older events that carry no durations.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from support_agent.domain.run_analysis import (
    EventView,
    RiskCode,
    analyze_run,
    recommendation,
    risk_flags,
)
from support_agent.domain.run_events import checked_sources
from support_agent.llm.usage import metered, timed

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
THRESHOLD = 0.7


def event(event_type: str, payload: dict[str, Any], actor: str = "system", i: int = 0) -> EventView:
    return EventView(event_type, actor, T0 + timedelta(seconds=i), payload)


def resolution(**overrides: Any) -> EventView:
    payload: dict[str, Any] = {
        "action": "refund",
        "reason_codes": [],
        "attempts": 1,
        "critic_verdicts": ["approve"],
        "validation_codes": [[]],
        "failure": None,
        "duration_ms": 900,
        "critic_ms": 300,
    }
    return event("resolution.completed", {**payload, **overrides}, "resolution_agent", 20)


def full_run() -> list[EventView]:
    return [
        event(
            "run.classified",
            {"category": "refund", "confidence": 0.95, "duration_ms": 120},
            "classifier",
            1,
        ),
        event(
            "supervisor.decided",
            {"run_investigator": True, "run_policy": True, "added_by_code": [], "duration_ms": 80},
            "supervisor",
            2,
        ),
        event("investigation.started", {}, "investigator", 3),
        event(
            "tool.called",
            {"tool": "get_order", "call_id": "call_1", "outcome": "ok", "latency_ms": 12},
            "investigator",
            4,
        ),
        event(
            "tool.called",
            {
                "tool": "get_shipment",
                "call_id": "required_get_shipment",
                "outcome": "ok",
                "latency_ms": 9,
            },
            "investigator",
            5,
        ),
        event(
            "investigation.completed",
            {
                "tool_calls": 2,
                "failed_calls": 0,
                "facts": 3,
                "dropped_facts": 1,
                "missing_information": 0,
                "missing_required": [],
                "duration_ms": 700,
            },
            "investigator",
            6,
        ),
        event(
            "tool.called",
            {"tool": "search_policy", "call_id": "search_1", "outcome": "ok"},
            "policy_agent",
            7,
        ),
        event(
            "policy.completed",
            {"tool_calls": 1, "searches": 1, "rules": 2, "dropped_rules": 1, "duration_ms": 400},
            "policy_agent",
            8,
        ),
        resolution(),
        event(
            "run.usage",
            {
                "llm_calls": 7,
                "input_tokens": 5000,
                "output_tokens": 600,
                "fallback_calls": 0,
                "active_ms": 2500,
            },
            i=30,
        ),
    ]


def codes(events: list[EventView]) -> list[RiskCode]:
    return [flag.code for flag in risk_flags(events, THRESHOLD)]


def test_a_clean_run_has_no_risk_flags() -> None:
    assert codes(full_run()) == []


def test_agents_appear_in_order_with_durations_and_code_written_counts() -> None:
    analysis = analyze_run(full_run(), THRESHOLD)
    rows = {row.agent: row for row in analysis.agents}
    assert [row.agent for row in analysis.agents] == [
        "classifier",
        "supervisor",
        "investigator",
        "policy_agent",
        "resolution_agent",
        "critic",
    ]
    assert rows["supervisor"].details["specialists"] == ["investigator", "policy_agent"]
    assert rows["investigator"].details["calls_by_code"] == 1
    assert rows["investigator"].duration_ms == 700
    assert rows["resolution_agent"].duration_ms == 900
    assert rows["critic"].duration_ms == 300
    assert rows["critic"].details == {"verdict": "approve", "reviews": 1}


def test_tools_quality_and_cost() -> None:
    analysis = analyze_run(full_run(), THRESHOLD)
    assert [(t.tool, t.by_code) for t in analysis.tools] == [
        ("get_order", False),
        ("get_shipment", True),
        ("search_policy", False),
    ]
    quality = analysis.quality
    assert quality.validator_passed is True
    assert quality.revisions == 0
    assert quality.critic_verdict == "approve"
    assert (quality.facts_grounded, quality.facts_dropped) == (3, 1)
    assert (quality.citations_verified, quality.citations_dropped) == (2, 1)
    assert analysis.cost.llm_calls == 7
    assert analysis.cost.active_ms == 2500


def test_delivery_date_conflict_is_flagged() -> None:
    """The validator's EVIDENCE_CONFLICT (order vs carrier delivery time) sends the case to a
    person; the analysis says why."""
    events = [
        *full_run()[:-2],
        resolution(
            action="escalate",
            reason_codes=["EVIDENCE_CONFLICT"],
            critic_verdicts=["not_run"],
            validation_codes=[["EVIDENCE_CONFLICT"]],
        ),
    ]
    assert codes(events) == [RiskCode.DELIVERY_DATE_CONFLICT]
    analysis = analyze_run(events, THRESHOLD)
    assert analysis.quality.validator_passed is False
    assert analysis.recommendation.accepted is False
    assert "critic" not in [row.agent for row in analysis.agents]


def test_injection_patterns_are_flagged_with_their_codes() -> None:
    events = [
        event("injection_suspected", {"source": "complaint", "codes": ["IGNORE_RULES"]}),
        *full_run(),
        event(
            "injection_suspected", {"source": "reply", "codes": ["FAKE_APPROVAL", "IGNORE_RULES"]}
        ),
    ]
    [flag] = risk_flags(events, THRESHOLD)
    assert flag.code == RiskCode.INJECTION_SUSPECTED
    assert flag.codes == ["FAKE_APPROVAL", "IGNORE_RULES"]
    assert flag.count == 2


def test_missing_required_evidence_is_flagged() -> None:
    events = full_run()
    events[5] = event(
        "investigation.completed",
        {"facts": 1, "missing_required": ["get_shipment"]},
        "investigator",
        6,
    )
    [flag] = risk_flags(events, THRESHOLD)
    assert (flag.code, flag.codes) == (RiskCode.MISSING_REQUIRED_EVIDENCE, ["get_shipment"])


@pytest.mark.parametrize(
    ("category", "confidence", "flagged"),
    [("refund", 0.69, True), ("refund", 0.7, False), ("unclear", 0.99, True)],
)
def test_low_classifier_confidence_uses_the_threshold(
    category: str, confidence: float, flagged: bool
) -> None:
    events = [event("run.classified", {"category": category, "confidence": confidence})]
    assert (RiskCode.LOW_CLASSIFIER_CONFIDENCE in codes(events)) is flagged


def test_provider_fallback_is_flagged_across_segments() -> None:
    events = [event("run.usage", {"fallback_calls": 1}), event("run.usage", {"fallback_calls": 2})]
    [flag] = risk_flags(events, THRESHOLD)
    assert (flag.code, flag.count) == (RiskCode.PROVIDER_FALLBACK, 3)


def test_a_revision_and_a_model_escalation() -> None:
    events = [
        resolution(
            action="escalate",
            reason_codes=["MODEL_ESCALATED"],
            attempts=2,
            critic_verdicts=["revise", "not_run"],
            validation_codes=[[], []],
        )
    ]
    analysis = analyze_run(events, THRESHOLD)
    assert analysis.quality.revisions == 1
    assert analysis.quality.critic_verdict == "revise"
    assert analysis.recommendation.accepted is True  # the model chose to escalate; code agreed


def test_events_from_before_durations_were_measured() -> None:
    events = [
        event("run.classified", {"category": "refund", "confidence": 0.9}),
        event("run.usage", {"llm_calls": 1}),
    ]
    analysis = analyze_run(events, THRESHOLD)
    assert analysis.agents[0].duration_ms is None
    assert analysis.cost.active_ms is None


def test_no_resolution_means_no_recommendation() -> None:
    rec = recommendation([event("run.classified", {"category": "unclear", "confidence": 0.2})])
    assert (rec.action, rec.accepted) == (None, False)


def test_checked_sources_are_fixed_codes_from_successful_calls() -> None:
    calls = [
        ("get_shipment", "ok"),
        ("get_order", "ok"),
        ("get_payment_status", "PAYMENT_UNAVAILABLE"),  # failed: not claimed as checked
        ("search_policy", "ok"),
        ("unknown_tool", "ok"),
    ]
    assert checked_sources(calls) == ["order", "shipment", "policy"]


def test_the_meter_adds_up_timed_steps() -> None:
    with metered() as meter:
        with timed("investigate"):
            pass
        with timed("investigate"):
            pass
    assert set(meter.durations_ms) == {"investigate"}
    assert meter.durations_ms["investigate"] >= 0


def test_timed_does_nothing_without_a_meter() -> None:
    with timed("classify"):
        pass  # no meter running: nothing to record, and no error
