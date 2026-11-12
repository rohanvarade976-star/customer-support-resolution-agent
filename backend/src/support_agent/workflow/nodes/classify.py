"""Classify: ask the classifier agent for a label, then check its order-number hint.

The model's order number is a hint, never a fact. It is used only when:
- the ticket names no order, and
- the order belongs to this run's customer (checked in the database).
Otherwise it is ignored, and `order_hint` records why.

LLM failures do not crash the graph: they become an error code in the state, and the
router sends the run to escalation.
"""

import re
from collections.abc import Awaitable, Callable
from uuid import UUID

from support_agent.agents.classifier import classify as run_classifier
from support_agent.llm.client import LLMClient
from support_agent.llm.errors import LLMUnavailableError, StructuredOutputError
from support_agent.workflow.state import OrderHint, WorkflowError, WorkflowState

# (customer_id, order_number) -> does this customer own that order?
OwnsOrder = Callable[[UUID, str], Awaitable[bool]]

# "ORD-1042", "ord 1042", "#1042", "1042" -> digits "1042". Anything else is not an order.
_ORDER_NUMBER = re.compile(r"^\s*(?:ord(?:er)?)?[\s#:-]*(\d{1,12})\s*$", re.IGNORECASE)


def normalize_order_number(raw: str | None) -> str | None:
    """The canonical "ORD-<digits>" form, or None if `raw` does not look like one."""
    if raw is None:
        return None
    match = _ORDER_NUMBER.match(raw)
    return f"ORD-{match.group(1)}" if match else None


async def resolve_order_hint(
    state: WorkflowState, hinted: str | None, owns_order: OwnsOrder
) -> tuple[OrderHint, str | None]:
    """(what happened to the hint, the order number the run should use)."""
    ticket_order = state["ticket_order_number"]
    hint = normalize_order_number(hinted)
    if hint is None:
        return OrderHint.NONE, ticket_order
    if ticket_order is not None:
        # The ticket's order was verified when the ticket was created: it always wins.
        outcome = (
            OrderHint.MATCHED_TICKET if hint == ticket_order else OrderHint.IGNORED_CONFLICTS_TICKET
        )
        return outcome, ticket_order
    if await owns_order(state["customer_id"], hint):
        return OrderHint.VERIFIED, hint
    return OrderHint.IGNORED_NOT_OWNED, None


async def classify(
    state: WorkflowState, *, llm: LLMClient, owns_order: OwnsOrder
) -> dict[str, object]:
    try:
        result = await run_classifier(llm, state["complaint_text"])
    except LLMUnavailableError:
        return {"classification": None, "errors": [WorkflowError.LLM_UNAVAILABLE]}
    except StructuredOutputError:
        return {"classification": None, "errors": [WorkflowError.CLASSIFIER_OUTPUT_INVALID]}

    order_hint, order_number = await resolve_order_hint(state, result.order_number, owns_order)
    return {"classification": result, "order_hint": order_hint, "order_number": order_number}
