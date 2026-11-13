"""Intake: the first node. Starts the run and fixes the order it is about (if known).

Later (M7) it will also load the customer's long-term memory as context.
"""

from support_agent.domain.enums import RunStatus
from support_agent.workflow.state import WorkflowState


async def intake(state: WorkflowState) -> dict[str, object]:
    return {
        "status": RunStatus.INVESTIGATING,
        # Until a hint is verified, the run works on the order the ticket names.
        "order_number": state["ticket_order_number"],
    }
