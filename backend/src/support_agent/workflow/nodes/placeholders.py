"""Stops that need no agent. Each only sets where the run stopped.

(The parameter must be named `state`: LangGraph's node type requires that name.)

- needs_clarification: the label is unclear or low-confidence. Replaced by the
  pause-and-ask loop in M7.
- ask_for_order: a confident label but no verified order number to work on. Same.

The escalation node lives in finish.py with the other outcomes.
"""

from support_agent.domain.enums import RunStatus
from support_agent.workflow.state import WorkflowState


async def needs_clarification(state: WorkflowState) -> dict[str, object]:
    return {"status": RunStatus.AWAITING_CUSTOMER}


async def ask_for_order(state: WorkflowState) -> dict[str, object]:
    return {"status": RunStatus.AWAITING_CUSTOMER, "missing_information": ["order_number"]}
