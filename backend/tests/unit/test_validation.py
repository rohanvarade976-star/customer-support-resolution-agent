"""domain/validation.py: pure checks on a drafted resolution. No LLM, no I/O."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from support_agent.domain.eligibility import EligibilityDecision, ReasonCode
from support_agent.domain.enums import ResolutionAction
from support_agent.domain.shipping import ShippingDelayFacts, compute_delay_facts
from support_agent.domain.validation import (
    ESCALATE_IMMEDIATELY,
    DeliveryDates,
    DraftClaim,
    ValidationCode,
    ValidationContext,
    claims_unverified_outcome,
    delivery_dates_conflict,
    normalize_citation,
    validate_resolution,
)

ELIGIBLE_MINOR = 129900
CITATION = "refund_policy_v2:section_3"
HONEST = (
    "Your return was received, so a refund is proposed and needs approval before it goes ahead."
)
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def context(**overrides: object) -> ValidationContext:
    base = ValidationContext(
        eligibility=EligibilityDecision(
            eligible=True, refundable_amount_minor=ELIGIBLE_MINOR, reasons=[]
        ),
        call_outcomes={"call_1": "ok", "call_2": "ok", "call_bad": "SHIPMENT_NOT_FOUND"},
        grounded_citations=frozenset({CITATION}),
        policy_conflict=False,
        no_relevant_policy=False,
        delivery_dates=None,
        delivery_tolerance=timedelta(hours=24),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def claim(**overrides: object) -> DraftClaim:
    base = DraftClaim(
        action=ResolutionAction.REFUND,
        amount_minor=ELIGIBLE_MINOR,
        cited_call_ids=("call_1",),
        policy_citations=(CITATION,),
        message=HONEST,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def not_eligible() -> EligibilityDecision:
    return EligibilityDecision(
        eligible=False, refundable_amount_minor=0, reasons=[ReasonCode.OUTSIDE_RETURN_WINDOW]
    )


# ---------------------------------------------------------------- a valid refund


def test_an_eligible_refund_for_the_exact_amount_passes() -> None:
    assert validate_resolution(claim(), context()) == []


# ---------------------------------------------------------------- amount and eligibility


def test_a_refund_for_the_wrong_amount_is_rejected() -> None:
    codes = validate_resolution(claim(amount_minor=ELIGIBLE_MINOR - 1), context())

    assert codes == [ValidationCode.REFUND_AMOUNT_MISMATCH]


def test_a_refund_without_an_amount_is_rejected() -> None:
    assert ValidationCode.REFUND_AMOUNT_MISMATCH in validate_resolution(
        claim(amount_minor=None), context()
    )


def test_a_refund_is_rejected_when_code_found_the_order_not_eligible() -> None:
    codes = validate_resolution(claim(amount_minor=0), context(eligibility=not_eligible()))

    assert ValidationCode.REFUND_NOT_ELIGIBLE in codes


def test_only_a_refund_may_carry_an_amount() -> None:
    codes = validate_resolution(claim(action=ResolutionAction.EXPLAIN_STATUS), context())

    assert codes == [ValidationCode.AMOUNT_NOT_ALLOWED]


def test_explaining_why_a_refund_is_not_possible_passes() -> None:
    explain = claim(
        action=ResolutionAction.EXPLAIN_STATUS,
        amount_minor=None,
        message="Returns are accepted within 30 days of delivery, and this order is past that.",
    )

    assert validate_resolution(explain, context(eligibility=not_eligible())) == []


# ---------------------------------------------------------------- cited evidence and policy


def test_a_call_id_that_does_not_exist_is_rejected() -> None:
    codes = validate_resolution(claim(cited_call_ids=("call_1", "call_99")), context())

    assert codes == [ValidationCode.CITED_CALL_NOT_FOUND]


def test_a_call_that_failed_is_rejected() -> None:
    codes = validate_resolution(claim(cited_call_ids=("call_bad",)), context())

    assert codes == [ValidationCode.CITED_CALL_FAILED]


def test_a_refund_must_cite_evidence() -> None:
    assert validate_resolution(claim(cited_call_ids=()), context()) == [
        ValidationCode.NO_EVIDENCE_CITED
    ]


def test_a_policy_citation_outside_the_grounded_findings_is_rejected() -> None:
    codes = validate_resolution(claim(policy_citations=(CITATION, "invented:section_9")), context())

    assert codes == [ValidationCode.CITATION_NOT_GROUNDED]


def test_a_refund_must_cite_a_policy_rule() -> None:
    assert validate_resolution(claim(policy_citations=()), context()) == [
        ValidationCode.NO_POLICY_CITED
    ]


def test_a_refund_is_rejected_when_no_policy_was_found() -> None:
    codes = validate_resolution(claim(), context(no_relevant_policy=True))

    assert ValidationCode.POLICY_NOT_FOUND in codes


# ---------------------------------------------------------------- conflicts


def test_meera_style_delivery_dates_that_differ_by_days_are_a_conflict() -> None:
    dates = DeliveryDates(
        order_delivered_at=datetime(2026, 11, 8, 12, tzinfo=UTC),
        shipment_delivered_at=datetime(2026, 11, 12, 12, tzinfo=UTC),
    )

    codes = validate_resolution(claim(), context(delivery_dates=dates))

    assert ValidationCode.EVIDENCE_CONFLICT in codes
    assert ValidationCode.EVIDENCE_CONFLICT in ESCALATE_IMMEDIATELY


def test_a_conflict_is_reported_even_if_the_draft_already_escalates() -> None:
    dates = DeliveryDates(NOW, NOW + timedelta(days=4))
    escalate = claim(
        action=ResolutionAction.ESCALATE, amount_minor=None, message="A person will help."
    )

    assert validate_resolution(escalate, context(delivery_dates=dates)) == [
        ValidationCode.EVIDENCE_CONFLICT
    ]


def test_dates_within_the_tolerance_are_not_a_conflict() -> None:
    tolerance = timedelta(hours=24)

    assert not delivery_dates_conflict(DeliveryDates(NOW, NOW + timedelta(hours=23)), tolerance)
    assert delivery_dates_conflict(DeliveryDates(NOW, NOW + timedelta(hours=25)), tolerance)


def test_one_side_saying_delivered_and_the_other_not_is_a_conflict() -> None:
    tolerance = timedelta(hours=24)

    assert delivery_dates_conflict(DeliveryDates(NOW, None), tolerance)
    assert delivery_dates_conflict(DeliveryDates(None, NOW), tolerance)
    assert not delivery_dates_conflict(DeliveryDates(None, None), tolerance)  # Arjun: in transit


def test_conflicting_policy_passages_force_escalation() -> None:
    codes = validate_resolution(claim(), context(policy_conflict=True))

    assert ValidationCode.POLICY_CONFLICT in codes
    assert ValidationCode.POLICY_CONFLICT in ESCALATE_IMMEDIATELY


# ---------------------------------------------------------------- the customer message


@pytest.mark.parametrize(
    "message",
    [
        "Your refund has been issued.",
        "Good news: your refund has already been processed.",
        "We have refunded your money.",
        "We've refunded you.",
        "I have issued your refund today.",
        "The refund is complete.",
        "Your refund was approved.",
        "Your refund will arrive within 5-7 business days.",
        "Your refund will reach you in 3 working days.",
        "Your money was refunded to your card.",
    ],
)
def test_a_message_claiming_an_unverified_outcome_is_rejected(message: str) -> None:
    assert claims_unverified_outcome(message)
    assert ValidationCode.UNVERIFIED_CLAIM in validate_resolution(claim(message=message), context())


@pytest.mark.parametrize(
    "message",
    [
        HONEST,
        "Returns are accepted within 30 days of delivery, and yours arrived on time.",
        "Your order is in transit and should arrive soon.",
        "We sent you an email about your return.",
    ],
)
def test_an_honest_message_passes_the_claim_check(message: str) -> None:
    assert not claims_unverified_outcome(message)


def test_the_message_may_state_only_the_eligible_amount_in_a_refund() -> None:
    right = claim(message="A refund of Rs. 1,299.00 is proposed and needs approval.")
    wrong = claim(message="A refund of Rs. 5,000 is proposed and needs approval.")

    assert validate_resolution(right, context()) == []
    assert validate_resolution(wrong, context()) == [ValidationCode.UNVERIFIED_AMOUNT]


def test_a_money_figure_is_never_allowed_outside_a_refund() -> None:
    explain = claim(
        action=ResolutionAction.EXPLAIN_STATUS,
        amount_minor=None,
        message="You paid ₹1,299.00 for this order.",
    )

    assert validate_resolution(explain, context()) == [ValidationCode.UNVERIFIED_AMOUNT]


def test_an_empty_message_is_rejected() -> None:
    assert ValidationCode.MESSAGE_EMPTY in validate_resolution(claim(message="  "), context())


def test_requesting_information_must_ask_a_question() -> None:
    ask = claim(
        action=ResolutionAction.REQUEST_INFO,
        amount_minor=None,
        cited_call_ids=(),
        policy_citations=(),
        message="Could you tell us which item is damaged?",
    )
    statement = replace(ask, message="Tell us which item is damaged.")

    assert validate_resolution(ask, context()) == []
    assert validate_resolution(statement, context()) == [ValidationCode.REQUEST_INFO_NO_QUESTION]


# ---------------------------------------------------------------- citations are normalized


@pytest.mark.parametrize(
    "wrapped",
    [
        f"[{CITATION}]",
        f'"{CITATION}"',
        f"'{CITATION}'",
        f"  [ {CITATION} ]  ",
        f"`{CITATION}`",
        CITATION,
    ],
)
def test_normalize_citation_strips_brackets_quotes_and_spaces(wrapped: str) -> None:
    assert normalize_citation(wrapped) == CITATION


def test_normalize_citation_leaves_the_inside_alone() -> None:
    assert normalize_citation("[refund_policy_v2:section_3 extra]") == (
        "refund_policy_v2:section_3 extra"
    )


# ---------------------------------------------------------------- promises and delay claims

DAY_COUNT_NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def delay(days_late: int) -> ShippingDelayFacts:
    return compute_delay_facts(
        expected_delivery=DAY_COUNT_NOW.date() - timedelta(days=days_late),
        delivered_at=None,
        carrier_status="in_transit",
        now=DAY_COUNT_NOW,
        threshold_days=7,
    )


def status_claim(
    message: str, action: ResolutionAction = ResolutionAction.EXPLAIN_STATUS
) -> DraftClaim:
    return claim(action=action, amount_minor=None, policy_citations=(), message=message)


ARJUN_DRAFT = (
    "Your laptop is over 7 days past the expected delivery date. "
    "An investigation will be started with the carrier."
)


def test_arjuns_draft_is_rejected_for_the_false_delay_and_the_promised_investigation() -> None:
    codes = validate_resolution(status_claim(ARJUN_DRAFT), context(delay_facts=delay(3)))

    assert ValidationCode.UNVERIFIED_CLAIM in codes
    assert ValidationCode.UNSUPPORTED_PROMISE in codes


def test_a_message_with_the_computed_delay_and_no_promise_passes() -> None:
    message = "Your laptop is 3 days past its expected delivery date and is still in transit."

    assert validate_resolution(status_claim(message), context(delay_facts=delay(3))) == []


@pytest.mark.parametrize(
    ("days_late", "passes"),
    [(7, False), (8, True)],  # strictly more than the 7-day threshold
)
def test_a_specialist_promise_needs_the_threshold_to_be_exceeded(
    days_late: int, passes: bool
) -> None:
    message = (
        f"Your shipment is {days_late} days past its expected date. "
        "A specialist is looking into it."
    )

    codes = validate_resolution(status_claim(message), context(delay_facts=delay(days_late)))

    assert (codes == []) is passes
    assert passes or codes == [ValidationCode.UNSUPPORTED_PROMISE]


def test_an_investigation_may_be_promised_when_the_threshold_is_exceeded() -> None:
    message = "Your shipment is 9 days past its expected date. We will open an investigation."

    assert validate_resolution(status_claim(message), context(delay_facts=delay(9))) == []


def test_escalation_wording_is_allowed_when_the_action_is_escalate() -> None:
    message = "A member of our support team will follow up with you."

    escalating = status_claim(message, ResolutionAction.ESCALATE)
    assert validate_resolution(escalating, context()) == []
    # The same words in an explain_status draft promise a handover nobody has decided.
    assert validate_resolution(status_claim(message), context()) == [
        ValidationCode.UNSUPPORTED_PROMISE
    ]


def test_an_escalate_draft_still_may_not_promise_an_investigation_or_compensation() -> None:
    for message in ("An investigation will be opened.", "We will send you a voucher."):
        codes = validate_resolution(status_claim(message, ResolutionAction.ESCALATE), context())

        assert codes == [ValidationCode.UNSUPPORTED_PROMISE]


def test_telling_the_customer_they_may_request_a_specialist_review_is_information() -> None:
    message = "Your laptop is still in transit. You can request a specialist review if you wish."

    assert validate_resolution(status_claim(message), context(delay_facts=delay(3))) == []


@pytest.mark.parametrize(
    "message",
    [
        "We will send you a voucher for the delay.",
        "We have arranged compensation for you.",
        "We will ship a replacement today.",
    ],
)
def test_compensation_is_never_allowed_even_when_an_investigation_applies(message: str) -> None:
    codes = validate_resolution(status_claim(message), context(delay_facts=delay(20)))

    assert codes == [ValidationCode.UNSUPPORTED_PROMISE]


def test_saying_we_cannot_offer_compensation_is_not_a_promise() -> None:
    message = "Our policy does not allow compensation for delays."

    assert validate_resolution(status_claim(message), context(delay_facts=delay(3))) == []


def test_a_days_late_figure_without_any_delay_facts_is_unverified() -> None:
    assert ValidationCode.UNVERIFIED_CLAIM in validate_resolution(
        status_claim("Your parcel is 5 days late."), context()
    )


def test_a_lower_bound_below_the_computed_delay_is_supported() -> None:
    message = "Your parcel is more than 2 days past its expected date."

    assert validate_resolution(status_claim(message), context(delay_facts=delay(3))) == []
