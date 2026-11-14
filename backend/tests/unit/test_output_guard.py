"""The output guardrail's checks (domain/output_guard.py) and their place in validation."""

import pytest

from support_agent.agents.resolve import ESCALATION_MESSAGE
from support_agent.domain.output_guard import ReplyIssue, check_reply
from support_agent.workflow.nodes.approval import DECLINED_MESSAGE
from support_agent.workflow.nodes.finish import REFUND_AWAITING_MESSAGE
from support_agent.workflow.nodes.placeholders import CLARIFY_MESSAGE, ORDER_NUMBER_MESSAGE

OWNED = {"ORD-2001", "ORD-2003"}


@pytest.mark.parametrize(
    "message",
    [
        ESCALATION_MESSAGE,
        CLARIFY_MESSAGE,
        ORDER_NUMBER_MESSAGE,
        REFUND_AWAITING_MESSAGE,
        DECLINED_MESSAGE,
        "Your order ORD-2001 was delivered on 2026-11-02, so it is past the return window.",
        "Your refund of Rs 3,49,900 is proposed and needs approval.",
        "A refund of Rs 249900.00 is proposed. Reference: order ORD-2003.",
        "It is 12 days late. Please allow 5 to 7 business days.",
        "We received your return on 14 November 2026 at 10:30.",
    ],
)
def test_ordinary_replies_pass(message: str) -> None:
    assert check_reply(message, OWNED) == []


@pytest.mark.parametrize(
    "message",
    [
        "Your case id is 3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b.",
        "As shown by call_abc123 your order was delivered.",
        "The tool_call_id says it was delivered.",
        "This follows refund_policy_v2:section_3.",
        "See section_3 of the policy.",
        "Rejected: REFUND_NOT_ELIGIBLE.",
        "The check CITATION_NOT_GROUNDED failed.",
    ],
)
def test_internal_identifiers_are_caught(message: str) -> None:
    assert check_reply(message, OWNED) == [ReplyIssue.INTERNAL_ID]


def test_internal_tokens_of_the_run_are_caught_literally() -> None:
    message = "Your order was delivered (see fetch-9f8e7d)."

    assert check_reply(message, OWNED) == []
    assert check_reply(message, OWNED, internal_tokens=["fetch-9f8e7d"]) == [ReplyIssue.INTERNAL_ID]
    assert check_reply("ok", OWNED, internal_tokens=["ok"]) == []  # too short to match on


@pytest.mark.parametrize(
    "message",
    [
        "Your order ORD-2002 is on its way.",
        "Order number 2002 was delivered.",
        "Both ord 2001 and ORD-2002 are in transit.",
    ],
)
def test_another_customers_order_is_caught(message: str) -> None:
    assert check_reply(message, OWNED) == [ReplyIssue.FOREIGN_ORDER]


def test_the_customers_own_orders_pass_in_any_spelling() -> None:
    assert check_reply("Order number 2001 and ord-2003 are fine.", OWNED) == []


def test_without_a_customer_context_the_order_check_is_skipped() -> None:
    assert check_reply("Your order ORD-9999 is on its way.", None) == []


@pytest.mark.parametrize(
    "message",
    [
        "Write to anita@example.com for help.",
        "Call us on +91 98765 43210.",
        "Call 9876543210 today.",
        "Our number is 987-654-3210.",
        "Phone 98765 43210 anytime.",
    ],
)
def test_email_addresses_and_phone_numbers_are_caught(message: str) -> None:
    assert check_reply(message, OWNED) == [ReplyIssue.CONTACT_DETAILS]


def test_several_problems_give_several_codes_and_never_the_matched_text() -> None:
    message = "ORD-2002 for a@b.com: 3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b"

    issues = check_reply(message, OWNED)

    assert issues == [
        ReplyIssue.INTERNAL_ID,
        ReplyIssue.FOREIGN_ORDER,
        ReplyIssue.CONTACT_DETAILS,
    ]
    assert all("a@b.com" not in issue.value for issue in issues)
