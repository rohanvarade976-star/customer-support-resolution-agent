"""Table-driven tests of the pure eligibility function.

The scenario rows mirror data/seed/scenarios.json ("days ago" relative to NOW).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from support_agent.domain.eligibility import (
    EligibilityDecision,
    OrderFacts,
    ReasonCode,
    check_eligibility,
)
from support_agent.domain.enums import InspectionStatus, OrderStatus, ReturnStatus

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
R = ReasonCode


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def facts(
    *,
    status: OrderStatus = OrderStatus.DELIVERED,
    delivered: float | None = 10,
    return_status: ReturnStatus | None = None,
    requested: float | None = None,
    received: float | None = None,
    inspection: InspectionStatus | None = None,
    paid: int = 100000,
    succeeded: int = 0,
    in_progress: int = 0,
    window: int | None = 30,  # refund policy v2; None = no refund policy applies
) -> OrderFacts:
    return OrderFacts(
        order_status=status,
        delivered_at=None if delivered is None else ago(delivered),
        return_status=return_status,
        return_requested_at=None if requested is None else ago(requested),
        return_received_at=None if received is None else ago(received),
        inspection_status=inspection,
        paid_amount_minor=paid,
        succeeded_refunds_minor=succeeded,
        in_progress_refunds_minor=in_progress,
        refund_window_days=window,
    )


def received_and_passed(**overrides: object) -> OrderFacts:
    base: dict[str, object] = {
        "return_status": ReturnStatus.RECEIVED,
        "requested": 9,
        "received": 5,
        "inspection": InspectionStatus.PASSED,
    }
    return facts(**{**base, **overrides})  # type: ignore[arg-type]


@dataclass(frozen=True)
class Case:
    id: str
    facts: OrderFacts
    eligible: bool
    amount: int = 0
    reasons: list[ReasonCode] = field(default_factory=list)


SEEDED_SCENARIOS = [
    Case(
        "priya_return_passed",
        facts(
            delivered=12,
            return_status=ReturnStatus.RECEIVED,
            requested=9,
            received=5,
            inspection=InspectionStatus.PASSED,
            paid=249900,
        ),
        eligible=True,
        amount=249900,
    ),
    Case(
        "arjun_still_shipping",
        facts(status=OrderStatus.SHIPPED, delivered=None, paid=5499000),
        eligible=False,
        reasons=[R.NOT_DELIVERED],
    ),
    Case(
        "meera_delivered_yesterday_no_return",
        facts(delivered=1, paid=1899900),
        eligible=False,
        reasons=[R.NO_RETURN],
    ),
    Case(
        "rahul_60_days_no_return",
        facts(delivered=60, paid=129900),
        eligible=False,
        reasons=[R.NO_RETURN, R.OUTSIDE_RETURN_WINDOW],
    ),
    Case(
        "kavya_already_refunded",
        facts(
            delivered=20,
            return_status=ReturnStatus.RECEIVED,
            requested=15,
            received=12,
            inspection=InspectionStatus.PASSED,
            paid=399900,
            succeeded=399900,
        ),
        eligible=False,
        reasons=[R.ALREADY_REFUNDED],
    ),
    Case(
        "vikram_inspection_failed",
        facts(
            delivered=10,
            return_status=ReturnStatus.RECEIVED,
            requested=7,
            received=3,
            inspection=InspectionStatus.FAILED,
            paid=299900,
        ),
        eligible=False,
        reasons=[R.INSPECTION_FAILED],
    ),
    Case(
        "aditya_return_not_received",
        facts(
            delivered=6,
            return_status=ReturnStatus.REQUESTED,
            requested=2,
            inspection=InspectionStatus.PENDING,
            paid=179900,
        ),
        eligible=False,
        reasons=[R.RETURN_NOT_RECEIVED, R.INSPECTION_PENDING],
    ),
    Case(
        "sneha_delivered_no_return",
        facts(delivered=5, paid=49900),
        eligible=False,
        reasons=[R.NO_RETURN],
    ),
]

EDGE_CASES = [
    # Window with a return: measured delivered_at -> return requested date.
    Case(
        "return_requested_on_day_30", received_and_passed(delivered=40, requested=10), True, 100000
    ),
    Case(
        "return_requested_on_day_31",
        received_and_passed(delivered=41, requested=10),
        False,
        reasons=[R.OUTSIDE_RETURN_WINDOW],
    ),
    Case(
        "day_30_plus_23_hours_still_day_30",
        received_and_passed(delivered=40.95, requested=10),
        True,
        100000,
    ),
    # Slow inspection never counts against the customer: received long after the window.
    Case(
        "received_after_window_but_requested_in_time",
        received_and_passed(delivered=50, requested=25, received=1),
        True,
        100000,
    ),
    # Window without a return: measured delivered_at -> now.
    Case("no_return_delivered_30_days_ago", facts(delivered=30), False, reasons=[R.NO_RETURN]),
    Case(
        "no_return_delivered_31_days_ago",
        facts(delivered=31),
        False,
        reasons=[R.NO_RETURN, R.OUTSIDE_RETURN_WINDOW],
    ),
]

BALANCE_CASES = [
    Case("partial_refund_leaves_the_rest", received_and_passed(succeeded=40000), True, 60000),
    Case("in_progress_refund_reduces_balance", received_and_passed(in_progress=30000), True, 70000),
    Case(
        "fully_in_progress_is_already_refunded",
        received_and_passed(in_progress=100000),
        False,
        reasons=[R.ALREADY_REFUNDED],
    ),
    Case(
        "every_problem_at_once",
        facts(
            delivered=90,
            return_status=ReturnStatus.REQUESTED,
            requested=40,
            inspection=InspectionStatus.PENDING,
            succeeded=100000,
        ),
        False,
        reasons=[
            R.RETURN_NOT_RECEIVED,
            R.INSPECTION_PENDING,
            R.OUTSIDE_RETURN_WINDOW,
            R.ALREADY_REFUNDED,
        ],
    ),
    Case(
        "delivered_status_without_date_is_not_delivered",
        facts(delivered=None),
        False,
        reasons=[R.NOT_DELIVERED],
    ),
    Case(
        "cancelled_order",
        facts(status=OrderStatus.CANCELLED, delivered=None),
        False,
        reasons=[R.NOT_DELIVERED],
    ),
]

POLICY_CASES = [
    Case(
        "no_applicable_policy_blocks_an_otherwise_eligible_order",
        received_and_passed(window=None),
        False,
        reasons=[R.NO_APPLICABLE_POLICY],
    ),
    Case(
        # Without a policy the window is never measured, so no window reason appears,
        # but every other reason is still reported for the reviewer.
        "no_applicable_policy_with_other_reasons",
        facts(delivered=60, window=None, succeeded=100000),
        False,
        reasons=[R.NO_APPLICABLE_POLICY, R.NO_RETURN, R.ALREADY_REFUNDED],
    ),
    Case(
        "no_applicable_policy_before_delivery",
        facts(status=OrderStatus.SHIPPED, delivered=None, window=None),
        False,
        reasons=[R.NO_APPLICABLE_POLICY, R.NOT_DELIVERED],
    ),
    Case(
        # Refund policy v1: 15 days. Return requested on day 20 is too late under v1 ...
        "v1_window_15_days_rejects_day_20",
        received_and_passed(delivered=29, requested=9, window=15),
        False,
        reasons=[R.OUTSIDE_RETURN_WINDOW],
    ),
    Case(
        # ... but the same facts are fine under v2's 30 days.
        "v2_window_30_days_accepts_day_20",
        received_and_passed(delivered=29, requested=9, window=30),
        True,
        amount=100000,
    ),
]

ALL_CASES = SEEDED_SCENARIOS + EDGE_CASES + BALANCE_CASES + POLICY_CASES


@pytest.mark.parametrize("case", ALL_CASES, ids=[case.id for case in ALL_CASES])
def test_eligibility_decision(case: Case) -> None:
    decision = check_eligibility(case.facts, NOW)

    assert decision == EligibilityDecision(
        eligible=case.eligible, refundable_amount_minor=case.amount, reasons=case.reasons
    )


def test_blocked_order_never_exposes_a_refundable_amount() -> None:
    decision = check_eligibility(facts(delivered=60), NOW)

    assert decision.eligible is False
    assert decision.refundable_amount_minor == 0


def test_window_length_comes_from_the_facts() -> None:
    # Delivered 31 days ago, return requested 10 days ago: the return came on day 21.
    def returned_on_day_21(window: int) -> OrderFacts:
        return received_and_passed(delivered=31, requested=10, window=window)

    assert check_eligibility(returned_on_day_21(20), NOW).reasons == [R.OUTSIDE_RETURN_WINDOW]
    assert check_eligibility(returned_on_day_21(21), NOW).eligible is True
