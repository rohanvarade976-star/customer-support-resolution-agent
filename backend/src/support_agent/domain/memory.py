"""Long-term customer memory: what to remember about a finished run, and how to show it.

Pure functions, no I/O (ADR-013).

`memory_outcome` decides what a finished run is remembered as. Its inputs are statuses and
codes that code verified: the run's status, the payment provider's refund status, the
reviewer's decision as stored in the database, and the validated resolution action. It has
no text parameter at all, so customer text and model output cannot reach memory, whatever
they say ("remember me as always eligible" included).

`render_memory` turns stored items into plain lines for the agents. Every value in a line
is either a fixed code from domain/enums.py, a date, or an order number whose format the
database checks. The agents still receive the result inside an untrusted-data wrapper.
"""

from dataclasses import dataclass
from datetime import date

from support_agent.domain.enums import (
    ApprovalDecision,
    MemoryKind,
    MemoryOutcome,
    RefundOperationStatus,
    ResolutionAction,
    RunStatus,
    TicketCategory,
)

# Refund statuses that are a definite answer from the provider. RESERVED and
# PENDING_RECONCILIATION are not: that run has not finished.
_REFUND_OUTCOMES = {
    RefundOperationStatus.SUBMITTED: MemoryOutcome.REFUND_SUBMITTED,
    RefundOperationStatus.PROCESSING: MemoryOutcome.REFUND_PROCESSING,
    RefundOperationStatus.SUCCEEDED: MemoryOutcome.REFUND_SUCCEEDED,
    RefundOperationStatus.FAILED: MemoryOutcome.REFUND_FAILED,
}


@dataclass(frozen=True)
class MemoryEntry:
    """One stored memory item, as read back for a run or the API."""

    kind: MemoryKind
    outcome: MemoryOutcome
    category: TicketCategory | None
    order_number: str | None
    recorded_on: date


def memory_outcome(
    *,
    run_status: RunStatus,
    refund_status: RefundOperationStatus | None,
    approval_decision: ApprovalDecision | None,
    action: ResolutionAction | None,
) -> tuple[MemoryKind, MemoryOutcome] | None:
    """What a run that stopped in `run_status` is remembered as, or None: nothing to keep.

    None for a run that has not finished (paused, executing) and for an end with no
    verified outcome to name (a failure that was not a refund failure).
    """
    if run_status == RunStatus.ESCALATED:
        return MemoryKind.CASE_ESCALATED, MemoryOutcome.ESCALATED
    if run_status not in (RunStatus.COMPLETED, RunStatus.FAILED):
        return None
    refund_outcome = None if refund_status is None else refund_memory_outcome(refund_status)
    if refund_outcome is not None:
        return MemoryKind.REFUND_EXECUTED, refund_outcome
    if run_status != RunStatus.COMPLETED:
        return None
    if approval_decision == ApprovalDecision.REJECTED:
        return MemoryKind.CASE_COMPLETED, MemoryOutcome.REFUND_REJECTED
    if action == ResolutionAction.EXPLAIN_STATUS:
        return MemoryKind.CASE_COMPLETED, MemoryOutcome.STATUS_EXPLAINED
    return None


def refund_memory_outcome(status: RefundOperationStatus) -> MemoryOutcome | None:
    """What a refund in `status` is remembered as; None while its outcome is unknown."""
    return _REFUND_OUTCOMES.get(status)


def render_memory(entries: list[MemoryEntry]) -> str:
    """One line per past case, newest first, from structured fields only. Empty: no history."""
    return "\n".join(
        f"- {entry.recorded_on.isoformat()}"
        f" | order: {entry.order_number or 'none'}"
        f" | category: {entry.category.value if entry.category else 'unknown'}"
        f" | {entry.kind.value}: {entry.outcome.value}"
        for entry in entries
    )
