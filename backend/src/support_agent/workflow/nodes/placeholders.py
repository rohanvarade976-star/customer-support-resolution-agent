"""End-of-graph placeholders for this task. Each only sets where the run stopped.

(The parameter must be named `state`: LangGraph's node type requires that name.)

- needs_clarification: replaced by the pause-and-ask loop in M7.
- escalate: hands the case to a human.
"""

from support_agent.domain.enums import RunStatus
from support_agent.workflow.state import WorkflowState


async def needs_clarification(state: WorkflowState) -> dict[str, object]:
    return {"status": RunStatus.AWAITING_CUSTOMER}


async def escalate(state: WorkflowState) -> dict[str, object]:
    return {"status": RunStatus.ESCALATED}
