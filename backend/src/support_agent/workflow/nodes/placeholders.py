"""Questions that need no agent: the run asks the customer a fixed question.

(The parameter must be named `state`: LangGraph's node type requires that name.)

- needs_clarification: the label is unclear or low-confidence.
- ask_for_order: a confident label but no verified order number to work on.

Each records what was asked (`asked_for`), then the graph pauses in `wait_for_customer`
(nodes/pauses.py). After the reply the run classifies again. The resolution's own questions
come from finish.request_info.
"""

from support_agent.domain.enums import RunStatus
from support_agent.workflow.state import AskedFor, WorkflowState

# PRD "Customer-facing language": fixed text, never model output.
CLARIFY_MESSAGE = "We need one more detail from you: could you describe what went wrong?"
ORDER_NUMBER_MESSAGE = "We need one more detail from you: which order is this about?"


async def needs_clarification(state: WorkflowState) -> dict[str, object]:
    return {
        "status": RunStatus.AWAITING_CUSTOMER,
        "asked_for": AskedFor.CLARIFICATION,
        "customer_reply": CLARIFY_MESSAGE,
    }


async def ask_for_order(state: WorkflowState) -> dict[str, object]:
    return {
        "status": RunStatus.AWAITING_CUSTOMER,
        "asked_for": AskedFor.ORDER_NUMBER,
        "missing_information": ["order_number"],
        "customer_reply": ORDER_NUMBER_MESSAGE,
    }
