"""What the agents did in a run, read from its audit trail (ADR-021). Pure functions, no I/O.

The reviewer's "AI analysis" is built here from audit events, which hold ids, codes, counts
and times only (services/run_recording.py). Everything returned is computed by code:

- agent activity: which agents ran, in order, how long each took and a few counts;
- tools used: tool names and outcome codes, and whether code (not the model) made the call;
- quality checks: the validator's result on the last draft, revisions, the critic's verdict,
  and how many facts and policy citations were grounded versus dropped by code;
- risk flags: fixed rules over the events. A model never raises or clears a flag;
- cost: LLM calls, tokens and active time.

The only model-written text a reviewer sees, the accepted draft's rationale, is added by the
service from the checkpoint and never passes through here. Nothing in this module can change
an amount or a decision: it only reads.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

# Code-made tool call ids (agents/required_evidence.py CALL_ID_PREFIX). Kept as a literal so
# the domain does not import an agent module.
_CODE_CALL_PREFIX = "required_"
# The validator code raised only by the delivery-date check (domain/validation.py).
_DELIVERY_CONFLICT_CODE = "EVIDENCE_CONFLICT"

Scalar = str | int | float | bool
Detail = Scalar | list[str]


class RiskCode(StrEnum):
    DELIVERY_DATE_CONFLICT = "DELIVERY_DATE_CONFLICT"  # order and carrier disagree
    INJECTION_SUSPECTED = "INJECTION_SUSPECTED"  # instruction-like customer text
    MISSING_REQUIRED_EVIDENCE = "MISSING_REQUIRED_EVIDENCE"  # a required lookup never succeeded
    LOW_CLASSIFIER_CONFIDENCE = "LOW_CLASSIFIER_CONFIDENCE"  # unclear or below the threshold
    PROVIDER_FALLBACK = "PROVIDER_FALLBACK"  # an answer came from the fallback LLM provider


# The event types the review queue needs for its per-row action and risk flags.
QUEUE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run.classified",
        "investigation.completed",
        "resolution.completed",
        "injection_suspected",
        "run.usage",
    }
)


@dataclass(frozen=True)
class EventView:
    """One audit event as this module needs it."""

    type: str
    actor: str
    at: datetime
    payload: dict[str, Any]


@dataclass(frozen=True)
class AgentActivity:
    agent: str  # "classifier", "supervisor", "investigator", "policy_agent", ...
    at: datetime  # when its result was recorded
    duration_ms: int | None  # None for runs recorded before durations were measured
    details: dict[str, Detail]  # codes and counts; the browser turns them into a sentence


@dataclass(frozen=True)
class ToolUse:
    call_id: str
    tool: str
    agent: str
    outcome: str  # "ok" or an error code
    latency_ms: int | None
    by_code: bool  # a required lookup code made itself, not the model


@dataclass(frozen=True)
class QualityChecks:
    attempts: int  # drafts written
    revisions: int
    # The validator on the last draft: True passed, False failed, None no draft was checked.
    validator_passed: bool | None
    validation_codes: list[str]  # the last draft's failed checks
    critic_verdict: str | None  # "approve", "revise", or None if the critic did not run
    facts_grounded: int
    facts_dropped: int
    citations_verified: int
    citations_dropped: int
    reply_replaced: bool  # the output guardrail replaced a reply


@dataclass(frozen=True)
class RiskFlag:
    code: RiskCode
    codes: list[str] = field(default_factory=list)  # e.g. injection pattern codes, tool names
    count: int = 1


@dataclass(frozen=True)
class RunCost:
    llm_calls: int
    input_tokens: int
    output_tokens: int
    active_ms: int | None  # None when no segment measured it
    fallback_calls: int


@dataclass(frozen=True)
class Recommendation:
    action: str | None  # the resolution agent's latest action; None if it never finished
    accepted: bool  # a draft passed every check (otherwise the case was escalated by code)
    reason_codes: list[str]


@dataclass(frozen=True)
class RunAnalysis:
    recommendation: Recommendation
    agents: list[AgentActivity]
    tools: list[ToolUse]
    quality: QualityChecks
    risk_flags: list[RiskFlag]
    cost: RunCost


def _int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _opt_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _strs(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _of_type(events: list[EventView], *types: str) -> list[EventView]:
    return [event for event in events if event.type in types]


def _latest(events: list[EventView], event_type: str) -> EventView | None:
    matching = _of_type(events, event_type)
    return matching[-1] if matching else None


def _verdicts(payload: dict[str, Any]) -> list[str]:
    return [v for v in _strs(payload, "critic_verdicts") if v != "not_run"]


def _by_code(event: EventView) -> bool:
    return str(event.payload.get("call_id", "")).startswith(_CODE_CALL_PREFIX)


def _agents(events: list[EventView]) -> list[AgentActivity]:
    """Agent rows in the order their results were recorded. Code-made lookups are counted
    per investigation pass (a run that loops back after a reply has several)."""
    rows: list[AgentActivity] = []
    code_calls = 0
    for event in events:
        if event.type == "tool.called" and _by_code(event):
            code_calls += 1
        rows.extend(_agent_rows(event, code_calls))
        if event.type == "investigation.completed":
            code_calls = 0
    return rows


def _agent_rows(event: EventView, code_calls: int) -> list[AgentActivity]:
    """The agent row(s) one audit event stands for. Most events are not agent results."""
    p = event.payload
    duration = _opt_int(p, "duration_ms")
    if event.type == "run.classified":
        details: dict[str, Detail] = {"category": str(p.get("category", ""))}
        if isinstance(p.get("confidence"), int | float):
            details["confidence"] = p["confidence"]
        return [AgentActivity("classifier", event.at, duration, details)]
    if event.type == "supervisor.decided":
        chosen = [
            name
            for name, key in (("investigator", "run_investigator"), ("policy_agent", "run_policy"))
            if p.get(key) is True
        ]
        details = {"specialists": chosen, "added_by_code": _strs(p, "added_by_code")}
        details["extra_round"] = p.get("extra_investigation_round") is True
        return [AgentActivity("supervisor", event.at, duration, details)]
    if event.type == "investigation.completed":
        details = {
            "tool_calls": _int(p, "tool_calls"),
            "failed_calls": _int(p, "failed_calls"),
            "facts": _int(p, "facts"),
            "dropped_facts": _int(p, "dropped_facts"),
            "calls_by_code": code_calls,
        }
        return [AgentActivity("investigator", event.at, duration, details)]
    if event.type == "policy.completed":
        details = {
            "searches": _int(p, "searches"),
            "rules": _int(p, "rules"),
            "dropped_rules": _int(p, "dropped_rules"),
            "conflicting_passages": p.get("conflicting_passages") is True,
            "no_relevant_policy": p.get("no_relevant_policy") is True,
        }
        return [AgentActivity("policy_agent", event.at, duration, details)]
    if event.type == "baseline.completed":
        details = {"tool_calls": _int(p, "tool_calls"), "failed_calls": _int(p, "failed_calls")}
        return [AgentActivity("baseline_agent", event.at, duration, details)]
    if event.type == "resolution.completed":
        attempts = _int(p, "attempts")
        details = {
            "action": str(p.get("action", "")),
            "attempts": attempts,
            "revisions": max(attempts - 1, 0),
        }
        if isinstance(p.get("failure"), str):
            details["failure"] = p["failure"]
        rows = [AgentActivity("resolution_agent", event.at, duration, details)]
        verdicts = _verdicts(p)
        if verdicts:  # the critic ran at least once
            critic: dict[str, Detail] = {"verdict": verdicts[-1], "reviews": len(verdicts)}
            rows.append(AgentActivity("critic", event.at, _opt_int(p, "critic_ms"), critic))
        return rows
    return []


def _tools(events: list[EventView]) -> list[ToolUse]:
    return [
        ToolUse(
            call_id=str(event.payload.get("call_id", "")),
            tool=str(event.payload.get("tool", "")),
            agent=event.actor,
            outcome=str(event.payload.get("outcome", "")),
            latency_ms=_opt_int(event.payload, "latency_ms"),
            by_code=_by_code(event),
        )
        for event in _of_type(events, "tool.called")
    ]


def _quality(events: list[EventView]) -> QualityChecks:
    resolution = _latest(events, "resolution.completed")
    investigation = _of_type(events, "investigation.completed")
    policy = _of_type(events, "policy.completed")
    attempts, validator_passed, last_codes, verdict = 0, None, [], None
    if resolution is not None:
        p = resolution.payload
        attempts = _int(p, "attempts")
        per_attempt = p.get("validation_codes")
        if isinstance(per_attempt, list) and per_attempt and isinstance(per_attempt[-1], list):
            last_codes = [code for code in per_attempt[-1] if isinstance(code, str)]
            validator_passed = not last_codes
        verdicts = _verdicts(p)
        verdict = verdicts[-1] if verdicts else None
    return QualityChecks(
        attempts=attempts,
        revisions=max(attempts - 1, 0),
        validator_passed=validator_passed,
        validation_codes=last_codes,
        critic_verdict=verdict,
        facts_grounded=sum(_int(e.payload, "facts") for e in investigation),
        facts_dropped=sum(_int(e.payload, "dropped_facts") for e in investigation),
        citations_verified=sum(_int(e.payload, "rules") for e in policy),
        citations_dropped=sum(_int(e.payload, "dropped_rules") for e in policy),
        reply_replaced=bool(_of_type(events, "reply.guardrail_replaced")),
    )


def _all_validation_codes(payload: dict[str, Any]) -> set[str]:
    codes = set(_strs(payload, "reason_codes"))
    per_attempt = payload.get("validation_codes")
    if isinstance(per_attempt, list):
        for attempt in per_attempt:
            if isinstance(attempt, list):
                codes.update(code for code in attempt if isinstance(code, str))
    return codes


def risk_flags(events: list[EventView], confidence_threshold: float) -> list[RiskFlag]:
    """Fixed rules over the audit events. The order is fixed too (RiskCode's order)."""
    flags: list[RiskFlag] = []

    conflicts = sum(
        1
        for event in _of_type(events, "resolution.completed")
        if _DELIVERY_CONFLICT_CODE in _all_validation_codes(event.payload)
    )
    if conflicts:
        flags.append(RiskFlag(RiskCode.DELIVERY_DATE_CONFLICT, [_DELIVERY_CONFLICT_CODE]))

    injections = _of_type(events, "injection_suspected")
    if injections:
        codes = sorted({code for event in injections for code in _strs(event.payload, "codes")})
        flags.append(RiskFlag(RiskCode.INJECTION_SUSPECTED, codes, len(injections)))

    missing = sorted(
        {
            tool
            for event in _of_type(events, "investigation.completed")
            for tool in _strs(event.payload, "missing_required")
        }
    )
    if missing:
        flags.append(RiskFlag(RiskCode.MISSING_REQUIRED_EVIDENCE, missing, len(missing)))

    low = [
        event
        for event in _of_type(events, "run.classified")
        if event.payload.get("category") == "unclear"
        or not isinstance(event.payload.get("confidence"), int | float)
        or event.payload["confidence"] < confidence_threshold
    ]
    if low:
        flags.append(RiskFlag(RiskCode.LOW_CLASSIFIER_CONFIDENCE, [], len(low)))

    usage = _of_type(events, "run.usage")
    fallbacks = sum(_int(event.payload, "fallback_calls") for event in usage)
    if fallbacks:
        flags.append(RiskFlag(RiskCode.PROVIDER_FALLBACK, [], fallbacks))
    return flags


def _cost(events: list[EventView]) -> RunCost:
    usage = _of_type(events, "run.usage")
    measured = [_opt_int(event.payload, "active_ms") for event in usage]
    active = [ms for ms in measured if ms is not None]
    return RunCost(
        llm_calls=sum(_int(event.payload, "llm_calls") for event in usage),
        input_tokens=sum(_int(event.payload, "input_tokens") for event in usage),
        output_tokens=sum(_int(event.payload, "output_tokens") for event in usage),
        active_ms=sum(active) if active else None,
        fallback_calls=sum(_int(event.payload, "fallback_calls") for event in usage),
    )


def recommendation(events: list[EventView]) -> Recommendation:
    """The resolution agent's latest action and whether a draft was accepted by code."""
    resolution = _latest(events, "resolution.completed")
    if resolution is None:
        return Recommendation(action=None, accepted=False, reason_codes=[])
    p = resolution.payload
    reasons = _strs(p, "reason_codes")
    action = p.get("action") if isinstance(p.get("action"), str) else None
    # Accepted: no reason codes, or only the model's own choice to escalate (agents/resolve.py).
    accepted = p.get("failure") is None and set(reasons) <= {"MODEL_ESCALATED"}
    return Recommendation(action=action, accepted=accepted, reason_codes=reasons)


def analyze_run(events: list[EventView], confidence_threshold: float) -> RunAnalysis:
    """The whole analysis from a run's events, oldest first."""
    return RunAnalysis(
        recommendation=recommendation(events),
        agents=_agents(events),
        tools=_tools(events),
        quality=_quality(events),
        risk_flags=risk_flags(events, confidence_threshold),
        cost=_cost(events),
    )
