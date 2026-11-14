"""The golden cases agree with the eval seed and with the eligibility rules' arithmetic.

If someone edits an order's price or a case's expected amount, this fails before a costly
real-model run scores a correct refund as unsafe.
"""

from collections import Counter

from evals.cases import EVAL_SEED_FILE, EVAL_SHIPMENTS_FILE, load_cases
from mock_providers.shipping.data import load_shipments
from support_agent.db.seed import SeedOrder, load_scenarios
from support_agent.domain.enums import OrderStatus, ResolutionAction

CASES = load_cases()
SEED = load_scenarios(EVAL_SEED_FILE)
ORDERS: dict[str, tuple[str, SeedOrder]] = {
    order.order_number: (customer.email, order)
    for customer in SEED.customers
    for order in customer.orders
}


def test_about_forty_cases_in_three_categories() -> None:
    counts = Counter(case.category for case in CASES)

    assert 35 <= len(CASES) <= 45
    assert set(counts) == {"normal", "hard", "adversarial"}
    assert all(n >= 10 for n in counts.values())
    assert {case.expected_action for case in CASES} == set(ResolutionAction)


def test_eval_customers_are_separate_from_the_demo_seed() -> None:
    demo = load_scenarios()
    demo_emails = {c.email for c in demo.customers}
    demo_orders = {o.order_number for c in demo.customers for o in c.orders}

    assert all(c.email.startswith("eval.") for c in SEED.customers)
    assert not demo_emails & {c.email for c in SEED.customers}
    assert not demo_orders & set(ORDERS)


def test_every_case_uses_an_eval_customer_and_their_own_order() -> None:
    emails = {c.email for c in SEED.customers}
    for case in CASES:
        assert case.customer_email in emails, case.id
        if case.order_number is not None:
            owner, _ = ORDERS[case.order_number]
            assert owner == case.customer_email, case.id


def test_expected_refunds_equal_the_full_unrefunded_order_amount() -> None:
    for case in CASES:
        if case.expected_action != ResolutionAction.REFUND:
            continue
        if case.order_number is not None:
            _, order = ORDERS[case.order_number]
        else:  # the number is only in the text: the customer must have just that one order
            (order,) = [o for email, o in ORDERS.values() if email == case.customer_email]
        assert order.refund_history is None, case.id
        assert case.expected_refund_minor == order.paid_amount_minor, case.id


def test_every_shipped_or_delivered_eval_order_has_a_carrier_record() -> None:
    shipments = load_shipments(EVAL_SHIPMENTS_FILE)
    needing = {
        number
        for number, (_, order) in ORDERS.items()
        if order.status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED)
    }

    assert needing <= set(shipments)
