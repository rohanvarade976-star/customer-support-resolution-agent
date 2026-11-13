"""domain/shipping.py: delay facts are computed from structured data, with a strict threshold."""

from datetime import UTC, date, datetime, timedelta

import pytest

from support_agent.domain.shipping import ShippingDelayFacts, compute_delay_facts

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
THRESHOLD = 7


def facts(
    days_late: int,
    *,
    delivered: bool = False,
    status: str = "in_transit",
    threshold: int | None = THRESHOLD,
) -> ShippingDelayFacts:
    expected: date = NOW.date() - timedelta(days=days_late)
    return compute_delay_facts(
        expected_delivery=expected,
        delivered_at=NOW - timedelta(days=1) if delivered else None,
        carrier_status=status,
        now=NOW,
        threshold_days=threshold,
    )


def test_arjuns_shipment_is_three_days_late_and_no_investigation_applies() -> None:
    result = facts(3)

    assert result.days_past_expected == 3
    assert result.investigation_applies is False
    assert result.investigation_threshold_days == THRESHOLD


@pytest.mark.parametrize(
    ("days_late", "applies"),
    [(6, False), (7, False), (8, True), (30, True)],  # "more than 7": 7 is not enough
)
def test_the_threshold_is_strictly_greater_than(days_late: int, applies: bool) -> None:
    assert facts(days_late).investigation_applies is applies


def test_a_shipment_that_is_not_late_has_zero_days() -> None:
    assert facts(-2).days_past_expected == 0  # expected in the future


def test_a_delivered_shipment_is_never_delayed() -> None:
    result = facts(20, delivered=True)

    assert result.delivered is True
    assert result.days_past_expected == 0
    assert result.investigation_applies is False


def test_a_carrier_reported_loss_qualifies_even_when_not_late() -> None:
    result = facts(0, status="Lost")

    assert result.carrier_reports_lost is True
    assert result.investigation_applies is True


def test_without_a_policy_threshold_no_investigation_applies() -> None:
    assert facts(30, threshold=None).investigation_applies is False
    assert facts(0, status="lost", threshold=None).investigation_applies is False
