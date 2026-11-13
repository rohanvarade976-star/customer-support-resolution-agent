"""After the reviewer's decision: fixed code only, no model (CLAUDE.md rules 1 and 5).

    wait_for_approval (pauses.py) -> check_approval -> execute_refund   (approved)
                                                    -> reopen_ticket    (rejected)
                                                    -> escalate         (expired, invalid, none)

- check_approval reads the decision stored in the database. The value the run was resumed
  with never decides anything.
- execute_refund is the only place in the workflow that can move money. It calls the refund
  executor, which revalidates the approval and the order, reserves the amount in a short
  locked transaction, and calls the provider once with a stable idempotency key. Running it
  twice for one proposal returns the first refund operation (and the database allows only
  one per proposal).
- Customer-facing text is the executor's description of the verified outcome, or a fixed line.

(The parameter must be named `state`: LangGraph's node type requires that name.)
"""

from support_agent.agents.resolve import ESCALATION_MESSAGE
from support_agent.core.errors import AppError
from support_agent.domain.enums import RunStatus, TicketStatus
from support_agent.domain.refund_outcomes import statuses_after_refund
from support_agent.workflow.deps import WorkflowDeps
from support_agent.workflow.state import WorkflowError, WorkflowState

# PRD "Customer-facing language": declined, with a next step.
DECLINED_MESSAGE = "Your refund request was declined. Our team will follow up on your ticket."


async def check_approval(state: WorkflowState, *, deps: WorkflowDeps) -> dict[str, object]:
    proposal_id = state.get("proposal_id")
    decision = None if proposal_id is None else await deps.load_approval_decision(proposal_id)
    return {"approval_decision": decision}


async def execute_refund(state: WorkflowState, *, deps: WorkflowDeps) -> dict[str, object]:
    proposal_id = state.get("proposal_id")
    if proposal_id is None:  # the router makes this unreachable
        raise RuntimeError("execute_refund needs a proposal id")
    try:
        result = await deps.execute_refund(proposal_id)
    except AppError as exc:
        # Revalidation refused it (expired, changed, no longer eligible, balance changed).
        # The executor has already recorded why; a person takes over.
        return {
            "status": RunStatus.ESCALATED,
            "ticket_status": TicketStatus.ESCALATED,
            "customer_reply": ESCALATION_MESSAGE,
            "refund_refusal": exc.error_code,
            "errors": [WorkflowError.REFUND_NOT_EXECUTED],
        }
    run_status, ticket_status = statuses_after_refund(result.status)
    return {
        "status": run_status,
        "ticket_status": ticket_status,
        "refund_status": result.status,
        "customer_reply": result.message,
    }


async def reopen_ticket(state: WorkflowState) -> dict[str, object]:
    """Rejected: no money moves. The run is done; the ticket is open again for a person."""
    return {
        "status": RunStatus.COMPLETED,
        "ticket_status": TicketStatus.OPEN,
        "customer_reply": DECLINED_MESSAGE,
    }
