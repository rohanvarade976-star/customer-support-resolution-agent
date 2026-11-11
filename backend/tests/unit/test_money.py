import pytest

from support_agent.domain.enums import RefundOperationStatus
from support_agent.domain.money import refund_totals, refundable_balance

S = RefundOperationStatus


@pytest.mark.parametrize(
    ("paid", "succeeded", "in_progress", "expected"),
    [
        (249900, 0, 0, 249900),  # nothing refunded yet
        (249900, 249900, 0, 0),  # fully refunded
        (249900, 100000, 0, 149900),  # partly refunded
        (249900, 0, 249900, 0),  # fully reserved or in flight
        (249900, 100000, 50000, 99900),  # both reduce the balance
        (1000, 800, 800, 0),  # over-committed data never goes negative
        (0, 0, 0, 0),
    ],
)
def test_refundable_balance(paid: int, succeeded: int, in_progress: int, expected: int) -> None:
    assert refundable_balance(paid, succeeded, in_progress) == expected


def test_refund_totals_counts_every_in_progress_status_but_not_failed() -> None:
    by_status = {
        S.SUCCEEDED: 100,
        S.RESERVED: 1,
        S.SUBMITTED: 2,
        S.PROCESSING: 4,
        S.PENDING_RECONCILIATION: 8,
        S.FAILED: 1000,  # moved no money
    }

    assert refund_totals(by_status) == (100, 15)


def test_refund_totals_of_nothing_is_zero() -> None:
    assert refund_totals({}) == (0, 0)
