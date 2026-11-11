"""Pure money rules. All amounts are integer minor units (paise), never floats."""

from collections.abc import Mapping

from support_agent.domain.enums import RefundOperationStatus

# Refunds that hold or may hold the customer's money. Each one reduces what is still
# refundable: reserved/submitted/processing are on their way, and a
# pending_reconciliation refund may already have happened (outcome unknown).
# FAILED refunds are not here: a failed refund moved no money.
IN_PROGRESS_REFUND_STATUSES = frozenset(
    {
        RefundOperationStatus.RESERVED,
        RefundOperationStatus.SUBMITTED,
        RefundOperationStatus.PROCESSING,
        RefundOperationStatus.PENDING_RECONCILIATION,
    }
)


def refund_totals(by_status: Mapping[RefundOperationStatus, int]) -> tuple[int, int]:
    """Split per-status refund sums into (succeeded, in_progress)."""
    succeeded = by_status.get(RefundOperationStatus.SUCCEEDED, 0)
    in_progress = sum(by_status.get(status, 0) for status in IN_PROGRESS_REFUND_STATUSES)
    return succeeded, in_progress


def refundable_balance(paid_minor: int, succeeded_minor: int, in_progress_minor: int) -> int:
    """What can still be refunded: paid - succeeded - in progress, never below 0."""
    return max(0, paid_minor - succeeded_minor - in_progress_minor)
