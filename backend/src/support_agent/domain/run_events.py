"""What a run's live status may show, and how it is written as Server-Sent Events (ADR-019).

Pure functions, no I/O. The audit trail is the source; this decides what leaves the API:

- Customers see only the event types listed in the PRD's "Live events" table, and never an
  event's payload. The browser turns a type into a fixed sentence ("Checking your order
  details"), so no stored text ever reaches the customer's timeline.
- Reviewers see every event type, with payload fields from a fixed list of keys whose values
  are ids, codes, counts or timestamps. Anything else, such as the reviewer's free-text note,
  is dropped. A value that does not look like a token (a sentence, for example) is dropped
  too, even under an allowed key, like the tracing allowlist (ADR-018).

The only free text in a run's live status is the ticket's customer reply, which the output
guardrail has already checked (ADR-017) and which states only verified outcomes.
"""

import json
import re
from typing import Any

from support_agent.domain.enums import RunStatus, TicketStatus

# The PRD's customer-facing events. Internal ones (tool calls, usage, supervisor steps,
# injection signals) are for reviewers, traces and logs.
CUSTOMER_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run.created",
        "run.classified",
        "investigation.started",
        "policy.completed",
        "clarification.requested",
        "proposal.created",
        "resolution.completed",
        "validation.passed",
        "validation.failed",
        "approval.requested",
        "approval.approved",
        "approval.rejected",
        "approval.expired",
        "refund.submitted",
        "refund.processing",
        "refund.succeeded",
        "refund.failed",
        "refund.reconciling",
        "ticket.updated",
        "run.completed",
        "run.escalated",
        "run.failed",
    }
)

# Payload keys a reviewer may see. Every one holds an id, a code, a count, a flag or a time.
# Deliberately absent: "note" (reviewer free text), "reviewer_id" and "actor_id" (staff
# identities), "idempotency_key".
REVIEWER_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "action",
        "added_by_code",
        "amount_minor",
        "approval_id",
        "attempts",
        "call_id",
        "category",
        "check",
        "checks",
        "codes",
        "confidence",
        "conflicting_passages",
        "critic_verdicts",
        "currency",
        "dropped_facts",
        "dropped_rules",
        "error_code",
        "expires_at",
        "extra_investigation_round",
        "facts",
        "failed_calls",
        "failure",
        "from_status",
        "input_tokens",
        "latency_ms",
        "llm_calls",
        "missing_information",
        "no_relevant_policy",
        "order_hint",
        "order_id",
        "outcome",
        "output_tokens",
        "payload_hash",
        "policy_versions",
        "proposal_id",
        "provider_reference",
        "reason",
        "reason_codes",
        "reasons",
        "refund_id",
        "resent",
        "resumed_by",
        "rules",
        "run_id",
        "searches",
        "source",
        "status",
        "ticket_id",
        "tool",
        "tool_calls",
        "validation_codes",
    }
)

# A token: letters, digits and _ . : / - +, no spaces, at most 80 characters. Ids, codes,
# citations ("refund_policy_v2:section_3") and ISO timestamps fit; a sentence does not.
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/+\-]{1,80}$")
_MAX_LIST_ITEMS = 20

# A finished run whose ticket is still in progress has a refund on its way (submitted or
# being reconciled): the stream stays open so the customer sees it succeed.
_FINISHED_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.ESCALATED, RunStatus.FAILED})

Scalar = str | int | float | bool


def is_finished(run_status: RunStatus, ticket_status: TicketStatus) -> bool:
    """Nothing more will happen on its own: the stream can end and the client stop polling."""
    return run_status in _FINISHED_RUN_STATUSES and ticket_status != TicketStatus.IN_PROGRESS


def visible_to_customer(event_type: str) -> bool:
    return event_type in CUSTOMER_EVENT_TYPES


def _safe_scalar(value: object) -> Scalar | None:
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str) and _TOKEN.fullmatch(value):
        return value
    return None


def _safe_value(value: object) -> Scalar | list[Scalar] | None:
    if isinstance(value, list):
        items = [_safe_scalar(item) for item in value[:_MAX_LIST_ITEMS]]
        return [item for item in items if item is not None]
    return _safe_scalar(value)  # dicts and anything else: None


def reviewer_details(payload: dict[str, Any]) -> dict[str, Scalar | list[Scalar]]:
    """The allowed keys of an audit payload, with values that look like ids or codes."""
    details: dict[str, Scalar | list[Scalar]] = {}
    for key in sorted(REVIEWER_PAYLOAD_KEYS.intersection(payload)):
        safe = _safe_value(payload[key])
        if safe is not None:
            details[key] = safe
    return details


def safe_actor(actor: str) -> str:
    """Actors are codes ("customer", "reviewer", "system"); anything else becomes "other"."""
    return actor if _TOKEN.fullmatch(actor) else "other"


def sse_frame(event: str, data: str, event_id: str | None = None) -> str:
    """One Server-Sent Event. `data` is a single line of JSON (no newlines inside)."""
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {data}\n\n"


def sse_comment(text: str) -> str:
    """A comment line: ignored by clients, keeps proxies from closing an idle connection."""
    return f": {text}\n\n"


def compact_json(value: object) -> str:
    # json.dumps escapes newlines inside strings, so the result is always one line.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
