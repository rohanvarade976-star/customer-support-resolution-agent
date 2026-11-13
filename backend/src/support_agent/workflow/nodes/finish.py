"""The end of the workflow: one node per outcome. Each sets where the run stops.

    refund        -> propose_refund: store the proposal and a pending approval. NO money moves.
                     The run then pauses for the reviewer (nodes/approval.py).
    explain       -> the validated reply is saved on the ticket; the run is completed.
    request_info  -> the validated question is saved; the run pauses for the customer
                     (nodes/pauses.py) and gathers again after the reply.
    escalate      -> a fixed message; a person takes over.

(The parameter must be named `state`: LangGraph's node type requires that name.)

Customer-facing text is either the validated draft message (explain, request_info) or a
fixed line written here, never an unchecked model message (CLAUDE.md rule 8).
The amount for a refund proposal comes from the eligibility decision via the outcome,
and the proposal service checks it against the database again before storing anything.
"""

from support_agent.agents.resolve import ESCALATION_MESSAGE
from support_agent.core.errors import AppError
from support_agent.domain.enums import RunStatus
from support_agent.workflow.deps import WorkflowDeps
from support_agent.workflow.state import AskedFor, WorkflowError, WorkflowState

# PRD "Customer-facing language".
REFUND_AWAITING_MESSAGE = "Your refund request is awaiting approval."


async def escalate(state: WorkflowState) -> dict[str, object]:
    return {"status": RunStatus.ESCALATED, "customer_reply": ESCALATION_MESSAGE}


async def propose_refund(state: WorkflowState, *, deps: WorkflowDeps) -> dict[str, object]:
    outcome = state.get("outcome")
    order_number = state.get("order_number")
    if outcome is None or outcome.amount_minor is None or order_number is None:
        return {**await escalate(state), "errors": [WorkflowError.PROPOSAL_REFUSED]}
    try:
        proposal_id = await deps.propose_refund(order_number, outcome.amount_minor)
    except AppError as exc:
        # Not eligible any more, an open proposal exists, or the amount changed: a person looks.
        return {
            **await escalate(state),
            "errors": [WorkflowError.PROPOSAL_REFUSED],
            "proposal_refusal": exc.error_code,
        }
    return {
        "status": RunStatus.AWAITING_APPROVAL,
        "proposal_id": proposal_id,
        "customer_reply": REFUND_AWAITING_MESSAGE,
    }


async def explain(state: WorkflowState) -> dict[str, object]:
    outcome = state.get("outcome")
    if outcome is None:  # the router makes this unreachable
        return await escalate(state)
    return {"status": RunStatus.COMPLETED, "customer_reply": outcome.message}


async def request_info(state: WorkflowState) -> dict[str, object]:
    outcome = state.get("outcome")
    if outcome is None:  # the router makes this unreachable
        return await escalate(state)
    return {
        "status": RunStatus.AWAITING_CUSTOMER,
        "asked_for": AskedFor.MORE_INFORMATION,
        "customer_reply": outcome.message,
    }
