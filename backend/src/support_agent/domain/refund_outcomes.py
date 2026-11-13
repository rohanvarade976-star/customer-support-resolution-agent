"""Where a run and its ticket stand after the payment provider's answer. Pure, no I/O.

Shared by the refund executor (services/refund_execution.py), which writes these statuses,
and the workflow's execute_refund node, which reports the same ones, so the two never drift.
"""

from support_agent.domain.enums import RefundOperationStatus, RunStatus, TicketStatus

# The provider took the refund on. Only SUCCEEDED means the money has arrived.
ACCEPTED_STATUSES = frozenset(
    {
        RefundOperationStatus.SUBMITTED,
        RefundOperationStatus.PROCESSING,
        RefundOperationStatus.SUCCEEDED,
    }
)


def statuses_after_refund(status: RefundOperationStatus) -> tuple[RunStatus, TicketStatus]:
    """(run status, ticket status) for a refund operation in `status`."""
    if status == RefundOperationStatus.SUCCEEDED:
        return RunStatus.COMPLETED, TicketStatus.RESOLVED  # the money has verifiably arrived
    if status in ACCEPTED_STATUSES:
        return RunStatus.COMPLETED, TicketStatus.IN_PROGRESS  # accepted, not yet paid out
    if status == RefundOperationStatus.FAILED:
        return RunStatus.FAILED, TicketStatus.ESCALATED
    # Reserved or pending reconciliation: the outcome is not known yet, the run keeps going.
    return RunStatus.EXECUTING, TicketStatus.IN_PROGRESS
