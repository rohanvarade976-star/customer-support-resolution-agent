"""Intake: the first node. Starts the run, fixes the order it is about (if known), and loads
the customer's long-term memory as background context.

Memory is loaded once, for the run's customer (the ticket's owner, set by code). It is plain
lines rendered by code from structured fields; the agents that see it wrap it as untrusted
data (ADR-013). A resumed run does not pass through intake again, so it keeps what it loaded.
"""

from support_agent.domain.enums import RunStatus
from support_agent.domain.injection import signal_for
from support_agent.workflow.deps import WorkflowDeps
from support_agent.workflow.state import WorkflowState


async def intake(state: WorkflowState, *, deps: WorkflowDeps) -> dict[str, object]:
    update: dict[str, object] = {
        "status": RunStatus.INVESTIGATING,
        # Until a hint is verified, the run works on the order the ticket names.
        "order_number": state["ticket_order_number"],
        "customer_memory": await deps.load_memory(state["customer_id"]),
        "owned_order_numbers": await deps.load_owned_order_numbers(state["customer_id"]),
    }
    # Flag instruction-like text. It is only a signal and a hint to the agents: the run
    # always continues (domain/injection.py, ADR-017).
    if signal := signal_for("complaint", state["complaint_text"]):
        update["injection_signals"] = [signal]
    return update
