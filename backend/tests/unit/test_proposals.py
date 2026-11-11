"""Canonical JSON, payload hashing and building a refund payload (pure functions)."""

import hashlib
from uuid import uuid4

import pytest

from support_agent.domain.eligibility import EligibilityDecision, ReasonCode
from support_agent.domain.proposals import (
    ELIGIBLE_CHECKS,
    build_refund_payload,
    canonical_json,
    payload_hash,
)


def test_canonical_json_sorts_keys_and_has_no_whitespace() -> None:
    assert canonical_json({"b": 1, "a": [1, 2], "c": {"y": 2, "x": 1}}) == (
        '{"a":[1,2],"b":1,"c":{"x":1,"y":2}}'
    )


def test_key_order_does_not_change_the_hash() -> None:
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})


def test_hash_is_sha256_of_the_canonical_json() -> None:
    expected = hashlib.sha256(b'{"amount_minor":249900}').hexdigest()
    assert payload_hash({"amount_minor": 249900}) == expected
    assert len(expected) == 64


def test_any_change_gives_a_different_hash() -> None:
    assert payload_hash({"amount_minor": 249900}) != payload_hash({"amount_minor": 249901})


def _decision(eligible: bool, amount: int) -> EligibilityDecision:
    reasons = [] if eligible else [ReasonCode.NO_RETURN]
    return EligibilityDecision(eligible=eligible, refundable_amount_minor=amount, reasons=reasons)


def test_payload_amount_comes_from_the_eligibility_decision() -> None:
    order_id, customer_id = uuid4(), uuid4()
    payload = build_refund_payload(
        order_id=order_id,
        order_number="ORD-1001",
        customer_id=customer_id,
        currency="INR",
        decision=_decision(eligible=True, amount=249900),
    ).to_json_dict()

    assert payload["action"] == "request_refund_approval"
    assert payload["amount_minor"] == 249900
    assert payload["order_id"] == str(order_id)
    assert payload["customer_id"] == str(customer_id)
    assert payload["checks_passed"] == list(ELIGIBLE_CHECKS)


def test_ineligible_decision_cannot_become_a_payload() -> None:
    with pytest.raises(ValueError, match="ineligible"):
        build_refund_payload(
            order_id=uuid4(),
            order_number="ORD-1004",
            customer_id=uuid4(),
            currency="INR",
            decision=_decision(eligible=False, amount=0),
        )
