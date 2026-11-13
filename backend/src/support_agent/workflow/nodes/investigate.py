"""Investigate: run the investigator agent on the run's order.

- No verified order number: skip the agent (no LLM call) and ask the customer for it.
- Otherwise: plan, tool loop, evidence (agents/investigator.py). The plan, the grounded
  evidence and the tool trace go into the state.
- If the agent stopped early (LLM down, invalid output, step limit) the node records an
  error code and the router sends the run to escalation. The trace is kept either way.

The customer id for the tools is taken from the state (the ticket's owner, matched to
the JWT when the run started), never from the model.
"""

from dataclasses import replace

from support_agent.agents.investigator import InvestigationFailure
from support_agent.agents.investigator import investigate as run_investigator
from support_agent.domain.enums import RunStatus
from support_agent.llm.client import LLMClient
from support_agent.tools.base import ToolContext, ToolRegistry
from support_agent.workflow.state import WorkflowError, WorkflowState

_ERROR_FOR_FAILURE = {
    InvestigationFailure.LLM_UNAVAILABLE: WorkflowError.LLM_UNAVAILABLE,
    InvestigationFailure.OUTPUT_INVALID: WorkflowError.INVESTIGATOR_OUTPUT_INVALID,
    InvestigationFailure.STEP_LIMIT: WorkflowError.INVESTIGATION_STEP_LIMIT,
}


async def investigate(
    state: WorkflowState,
    *,
    llm: LLMClient,
    registry: ToolRegistry,
    ctx: ToolContext,
    max_tool_calls: int,
) -> dict[str, object]:
    order_number = state.get("order_number")
    if order_number is None:
        return {"status": RunStatus.AWAITING_CUSTOMER, "missing_information": ["order_number"]}

    outcome = await run_investigator(
        llm,
        registry,
        replace(ctx, customer_id=state["customer_id"]),
        complaint_text=state["complaint_text"],
        order_number=order_number,
        max_tool_calls=max_tool_calls,
    )
    update: dict[str, object] = {
        "investigation_plan": outcome.plan,
        "evidence": outcome.evidence,
        "tool_trace": outcome.trace,
    }
    if outcome.failure is not None:
        update["errors"] = [_ERROR_FOR_FAILURE[outcome.failure]]
    return update
