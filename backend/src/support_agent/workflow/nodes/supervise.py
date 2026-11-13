"""Supervise: choose which specialists gather information (agents/supervisor.py).

The node only records the plan. `route_after_supervise` (graph.py, plain code) turns it
into the branches that run. A supervisor failure (LLM down, invalid output) becomes an error
code and the run escalates, like every other LLM failure.
"""

from support_agent.agents.supervisor import supervise as run_supervisor
from support_agent.domain.enums import TicketCategory
from support_agent.llm.errors import LLMUnavailableError, StructuredOutputError
from support_agent.workflow.deps import WorkflowDeps
from support_agent.workflow.state import WorkflowError, WorkflowState


async def supervise(state: WorkflowState, *, deps: WorkflowDeps) -> dict[str, object]:
    classification = state.get("classification")
    order_number = state.get("order_number")
    if classification is None or order_number is None:  # the router makes this unreachable
        return {"errors": [WorkflowError.SUPERVISOR_OUTPUT_INVALID]}
    try:
        plan = await run_supervisor(
            deps.llm,
            complaint_text=state["complaint_text"],
            category=TicketCategory(classification.category),
            order_number=order_number,
            tier=deps.tool_context.settings.supervisor_tier,
        )
    except LLMUnavailableError:
        return {"errors": [WorkflowError.LLM_UNAVAILABLE]}
    except StructuredOutputError:
        return {"errors": [WorkflowError.SUPERVISOR_OUTPUT_INVALID]}
    return {"supervisor": plan}
