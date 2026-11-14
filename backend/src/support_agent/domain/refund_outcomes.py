"""Where a run and its ticket stand after the payment provider's answer. Pure, no I/O.

Shared by the refund executor (services/refund_execution.py), which writes these statuses,
the reconciler (services/reconciliation.py), which settles them later, and the workflow's
execute_refund node, which reports the same ones, so the three never drift.
"""

from support_agent.domain.enums import RefundOperationStatus, RunStatus, TicketStatus
from support_agent.domain.money import IN_PROGRESS_REFUND_STATUSES

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


# The provider's last word on a refund: nothing changes after these.
FINAL_STATUSES = frozenset({RefundOperationStatus.SUCCEEDED, RefundOperationStatus.FAILED})
# Statuses the reconciler still has work to do for: the same ones that still hold the
# customer's money in the balance check.
UNSETTLED_STATUSES = IN_PROGRESS_REFUND_STATUSES
# How far along each status is, so a check can only move a refund forward.
_PROGRESS = {
    RefundOperationStatus.RESERVED: 0,
    RefundOperationStatus.PENDING_RECONCILIATION: 0,
    RefundOperationStatus.SUBMITTED: 1,
    RefundOperationStatus.PROCESSING: 2,
    RefundOperationStatus.SUCCEEDED: 3,
    RefundOperationStatus.FAILED: 3,
}


def status_to_adopt(
    current: RefundOperationStatus, reported: RefundOperationStatus | None
) -> RefundOperationStatus | None:
    """The status reconciliation records after a check, or None: nothing new was learned.

    `reported` is what the provider said (None: no clear answer). PENDING_RECONCILIATION is
    what an unrecognised provider word maps to, so it is not an answer either. A report that
    would move the refund backwards (processing -> submitted) is ignored.
    """
    if reported is None or reported == RefundOperationStatus.PENDING_RECONCILIATION:
        return None
    if current in FINAL_STATUSES or _PROGRESS[reported] <= _PROGRESS[current]:
        return None
    return reported
