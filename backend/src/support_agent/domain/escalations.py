"""Why a run was escalated, as plain reason codes read from its audit trail (ADR-022).

A pure function, no I/O. The audit events already hold codes only (services/run_recording.py),
so the reasons are codes too: the escalations inbox turns each into a fixed sentence. No model
text is read, and a model cannot add or remove a reason.
"""

from enum import StrEnum

from support_agent.domain.run_analysis import EventView, risk_flags

# The resolution agent's "the draft itself chose escalate" code (agents/resolve.py). Kept as a
# literal so the domain does not import an agent module.
_MODEL_ESCALATED = "MODEL_ESCALATED"
_LLM_UNAVAILABLE = "LLM_UNAVAILABLE"


class EscalationCode(StrEnum):
    AI_CHOSE_TO_ESCALATE = "AI_CHOSE_TO_ESCALATE"  # the AI's own accepted choice
    DRAFT_FAILED_CHECKS = "DRAFT_FAILED_CHECKS"  # code rejected every draft (validator, critic)
    AI_UNAVAILABLE = "AI_UNAVAILABLE"  # every LLM provider failed
    WORKFLOW_ERROR = "WORKFLOW_ERROR"  # a step failed (codes say which)
    PROPOSAL_REFUSED = "PROPOSAL_REFUSED"  # code refused to store the refund proposal
    REFUND_NOT_EXECUTED = "REFUND_NOT_EXECUTED"  # revalidation refused an approved refund
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"  # nobody decided the refund in time
    JOB_FAILED = "JOB_FAILED"  # a background job kept failing
    REPLY_BLOCKED = "REPLY_BLOCKED"  # the output guardrail replaced the reply
    RISK_FLAG = "RISK_FLAG"  # code-computed risk flags (ADR-021)
    UNKNOWN = "UNKNOWN"  # escalated, but no recorded reason (older runs)


def _strs(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _from_run_escalated(event: EventView) -> list[tuple[EscalationCode, list[str]]]:
    payload = event.payload
    found: list[tuple[EscalationCode, list[str]]] = []
    if payload.get("reason") == "JOB_DEAD":
        found.append((EscalationCode.JOB_FAILED, _strs([payload.get("job_type")])))
    if isinstance(payload.get("proposal_refusal"), str):
        found.append((EscalationCode.PROPOSAL_REFUSED, [payload["proposal_refusal"]]))
    if isinstance(payload.get("refund_refusal"), str):
        found.append((EscalationCode.REFUND_NOT_EXECUTED, [payload["refund_refusal"]]))
    errors = [
        code
        for code in _strs(payload.get("error_codes"))
        if code not in ("PROPOSAL_REFUSED", "REFUND_NOT_EXECUTED")  # reported above
    ]
    if _LLM_UNAVAILABLE in errors:
        found.append((EscalationCode.AI_UNAVAILABLE, []))
        errors.remove(_LLM_UNAVAILABLE)
    if errors:
        found.append((EscalationCode.WORKFLOW_ERROR, errors))
    return found


def _from_resolution(event: EventView) -> list[tuple[EscalationCode, list[str]]]:
    reasons = _strs(event.payload.get("reason_codes"))
    if _MODEL_ESCALATED in reasons:
        return [(EscalationCode.AI_CHOSE_TO_ESCALATE, [])]
    if event.payload.get("failure") is not None:  # reported through run.escalated's codes
        return []
    return [(EscalationCode.DRAFT_FAILED_CHECKS, reasons)] if reasons else []


def escalation_reasons(
    events: list[EventView], confidence_threshold: float
) -> list[tuple[EscalationCode, list[str]]]:
    """(code, detail codes) pairs for an escalated run, in a fixed order, without repeats."""
    found: dict[EscalationCode, list[str]] = {}

    def add(code: EscalationCode, details: list[str]) -> None:
        merged = found.setdefault(code, [])
        merged.extend(d for d in details if d not in merged)

    resolutions = [event for event in events if event.type == "resolution.completed"]
    if resolutions:
        for code, details in _from_resolution(resolutions[-1]):
            add(code, details)
    for event in events:
        if event.type == "run.escalated":
            for code, details in _from_run_escalated(event):
                add(code, details)
        elif event.type == "approval.expired":
            add(EscalationCode.APPROVAL_EXPIRED, [])
        elif event.type == "reply.guardrail_replaced":
            add(EscalationCode.REPLY_BLOCKED, _strs(event.payload.get("codes")))
    flags = [flag.code.value for flag in risk_flags(events, confidence_threshold)]
    if flags:
        add(EscalationCode.RISK_FLAG, flags)
    if not found:
        add(EscalationCode.UNKNOWN, [])
    order = list(EscalationCode)
    return sorted(found.items(), key=lambda item: order.index(item[0]))
