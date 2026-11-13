"""The two places a run pauses: waiting for the reviewer, and waiting for the customer.

How a pause works (LangGraph `interrupt`): the node calls `interrupt(...)`, the checkpointer
saves the run, and the graph call returns. Later, `Command(resume=value)` on the same thread
runs this node AGAIN FROM ITS FIRST LINE, and this time `interrupt` returns `value`.

So nothing may happen before `interrupt` that must not happen twice: no database writes, no
provider calls, no LLM calls. Both nodes start with the `interrupt` call itself. Interrupt
payloads hold a reason and ids only, never customer text.

(The parameter must be named `state`: LangGraph's node type requires that name.)
"""

from typing import Any

from langgraph.types import interrupt

from support_agent.domain.enums import RunStatus
from support_agent.workflow.state import AskedFor, WorkflowState

AWAITING_APPROVAL = "awaiting_approval"
AWAITING_CUSTOMER = "awaiting_customer"

# Reset before a new gathering pass, so it never reuses an earlier pass's evidence.
_GATHERING_RESET: dict[str, object] = {
    "investigation": None,
    "investigation_retry": None,
    "policy": None,
    "baseline_messages": None,
}


async def wait_for_approval(state: WorkflowState) -> dict[str, object]:
    """Pause until a reviewer decides. The resume value is ignored on purpose: the next
    node reads the decision from the database, where the decision endpoint stored it."""
    proposal_id = state.get("proposal_id")
    interrupt({"reason": AWAITING_APPROVAL, "proposal_id": str(proposal_id)})
    return {"status": RunStatus.EXECUTING}


def _reply_text(value: Any) -> str:
    """The reply the run was resumed with. Anything else is a bug in our own code."""
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise ValueError("a customer resume value must be {'text': str}")
    text: str = value["text"]
    return text


async def wait_for_customer(state: WorkflowState) -> dict[str, object]:
    """Pause until the customer replies, then keep the reply as untrusted text."""
    reply = _reply_text(interrupt({"reason": AWAITING_CUSTOMER}))
    update: dict[str, object] = {
        "status": RunStatus.INVESTIGATING,
        "customer_replies": [reply],  # appended (the key has a reducer)
        "clarification_rounds": state.get("clarification_rounds", 0) + 1,
    }
    if state.get("asked_for") == AskedFor.MORE_INFORMATION:
        update.update(_GATHERING_RESET)  # the run gathers again with the new information
    return update
