"""The state that flows through the LangGraph workflow.

LangGraph keeps one state dict per run. Each node receives the whole state and returns
only the keys it changed; LangGraph merges them in. For most keys "merge" means
"replace". `errors` has a reducer (`operator.add`), so a node returns only its *new*
errors and they are appended to the list.

The two specialists run in parallel (graph.py). They never write the same key: the
investigator writes `investigation`, the policy agent writes `policy`, and both may append
to `errors`, the one key with a reducer. That is what makes the parallel merge safe.

From M7 the state is saved by the checkpointer after every step (workflow/checkpointer.py),
so a paused run can be resumed later. Checkpoints hold customer text and model output: they
are working memory for the run, never a system of record, and never copied into audit
events. services/run_recording.py turns what each part of a run did into audit events (ids,
counts and codes only) and rows.

A run can loop back after the customer replies. Values that belong to one gathering pass
(`investigation`, `investigation_retry`, `policy`, `baseline_messages`) are reset to None
before gathering again, so a later pass never reuses an earlier pass's evidence.
"""

import operator
from enum import StrEnum
from typing import Annotated, NotRequired, TypedDict, cast
from uuid import UUID

from support_agent.agents.baseline import BaselineGathering
from support_agent.agents.classifier import Classification
from support_agent.agents.investigator import InvestigationOutcome
from support_agent.agents.policy import PolicyOutcome
from support_agent.agents.resolve import ResolutionOutcome
from support_agent.agents.supervisor import SupervisorPlan
from support_agent.domain.eligibility import EligibilityDecision
from support_agent.domain.enums import (
    ApprovalDecision,
    RefundOperationStatus,
    RunStatus,
    TicketStatus,
)
from support_agent.domain.shipping import ShippingDelayFacts


class OrderHint(StrEnum):
    """What happened to the order number the classifier extracted (it is only a hint)."""

    NONE = "none"  # the classifier found no order number
    MATCHED_TICKET = "matched_ticket"  # same order the ticket already names
    VERIFIED = "verified"  # ticket names no order; this customer owns the hinted one
    IGNORED_NOT_OWNED = "ignored_not_owned"  # unknown, or another customer's order
    IGNORED_CONFLICTS_TICKET = "ignored_conflicts_ticket"  # ticket names a different order


class AskedFor(StrEnum):
    """What the run asked the customer, which decides where it continues after the reply."""

    CLARIFICATION = "clarification"  # unclear or low-confidence label -> classify again
    ORDER_NUMBER = "order_number"  # no verified order -> classify again
    MORE_INFORMATION = "more_information"  # the resolution asked a question -> gather again


class WorkflowError(StrEnum):
    """Error codes a node can record. Codes only: never model output or customer text.

    The values of the agents' own failure enums (InvestigationFailure, PolicyFailure,
    ResolutionFailure, BaselineFailure) are all members here, so `WorkflowError(value)` works.
    """

    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"  # every provider failed (LLMUnavailableError)
    CLASSIFIER_OUTPUT_INVALID = "CLASSIFIER_OUTPUT_INVALID"  # StructuredOutputError
    SUPERVISOR_OUTPUT_INVALID = "SUPERVISOR_OUTPUT_INVALID"
    INVESTIGATOR_OUTPUT_INVALID = "INVESTIGATOR_OUTPUT_INVALID"  # plan, tool call or summary
    INVESTIGATION_STEP_LIMIT = "INVESTIGATION_STEP_LIMIT"  # the model wanted too many tool calls
    POLICY_OUTPUT_INVALID = "POLICY_OUTPUT_INVALID"
    POLICY_STEP_LIMIT = "POLICY_STEP_LIMIT"
    BASELINE_OUTPUT_INVALID = "BASELINE_OUTPUT_INVALID"
    BASELINE_STEP_LIMIT = "BASELINE_STEP_LIMIT"
    RESOLUTION_OUTPUT_INVALID = "RESOLUTION_OUTPUT_INVALID"
    CRITIC_OUTPUT_INVALID = "CRITIC_OUTPUT_INVALID"
    CASE_FACTS_UNAVAILABLE = "CASE_FACTS_UNAVAILABLE"  # eligibility could not be computed
    PROPOSAL_REFUSED = "PROPOSAL_REFUSED"  # code refused to store the refund proposal
    REFUND_NOT_EXECUTED = "REFUND_NOT_EXECUTED"  # revalidation refused the approved refund


class WorkflowState(TypedDict):
    # --- Set by code when the run starts (services/runs.py); never by a model ---
    run_id: UUID
    ticket_id: UUID
    customer_id: UUID  # the ticket's owner, already matched to the JWT
    complaint_text: str  # untrusted customer text: only ever sent as a user message
    ticket_order_number: str | None  # the order the ticket names, if any

    # --- Filled in by nodes ---
    status: RunStatus
    classification: NotRequired[Classification | None]  # None if the LLM call failed
    # The order the rest of the run works on: the ticket's, or a verified hint.
    order_number: NotRequired[str | None]
    order_hint: NotRequired[OrderHint]
    missing_information: NotRequired[list[str]]  # what we must ask the customer for

    # Gathering. Present exactly when that step ran, which is how services/runs.py knows
    # which events to write.
    supervisor: NotRequired[SupervisorPlan]
    # None = reset before a new gathering pass (see the module docstring).
    investigation: NotRequired[InvestigationOutcome | None]  # after the required-evidence step
    investigation_retry: NotRequired[InvestigationOutcome | None]  # the one extra round, if any
    retry_gaps: NotRequired[list[str]]  # the required lookups the extra round is looking for
    retry_investigation: NotRequired[bool]  # set by the gather node, read by the router
    policy: NotRequired[PolicyOutcome | None]
    baseline_messages: NotRequired[BaselineGathering | None]  # baseline mode only

    # Computed by code, never by a model.
    eligibility: NotRequired[EligibilityDecision]
    delay_facts: NotRequired[ShippingDelayFacts | None]

    # Resolution and what followed.
    outcome: NotRequired[ResolutionOutcome]
    proposal_id: NotRequired[UUID]
    customer_reply: NotRequired[str]  # saved on the ticket: only verified outcomes
    proposal_refusal: NotRequired[str]  # the error code, if code refused the proposal

    # Waiting for the customer (M7). Replies are untrusted text, like the complaint.
    asked_for: NotRequired[AskedFor]
    customer_replies: Annotated[list[str], operator.add]
    clarification_rounds: NotRequired[int]  # questions answered so far

    # After the reviewer's decision (M7). Read from the database by code, never resumed in.
    approval_decision: NotRequired[ApprovalDecision | None]
    refund_status: NotRequired[RefundOperationStatus]  # what the payment provider said
    refund_refusal: NotRequired[str]  # the error code, if revalidation refused the refund
    # Set only where the ticket's status does not follow from the run's (after a decision).
    ticket_status: NotRequired[TicketStatus]
    errors: Annotated[list[WorkflowError], operator.add]


# Top-level enum fields. A checkpoint stores a StrEnum as a plain string, so these come back
# as `str`; `restore_enums` turns them back into members after reading a checkpoint.
_ENUM_FIELDS: dict[str, type[StrEnum]] = {
    "status": RunStatus,
    "order_hint": OrderHint,
    "asked_for": AskedFor,
    "approval_decision": ApprovalDecision,
    "refund_status": RefundOperationStatus,
    "ticket_status": TicketStatus,
}


def restore_enums(values: dict[str, object]) -> WorkflowState:
    """The state read from a checkpoint, with its enum fields typed again."""
    restored = dict(values)
    for key, enum in _ENUM_FIELDS.items():
        value = restored.get(key)
        if isinstance(value, str):
            restored[key] = enum(value)
    errors = restored.get("errors")
    if isinstance(errors, list):
        restored["errors"] = [WorkflowError(code) for code in errors]
    return cast(WorkflowState, restored)


def customer_text(state: WorkflowState) -> str:
    """The complaint plus every reply so far: what the agents read as the customer's words.

    Still untrusted data: each agent wraps it with `wrap_untrusted` before sending it.
    """
    replies = state.get("customer_replies") or []
    parts = [state["complaint_text"]]
    parts += [f"Customer follow-up {i}: {reply}" for i, reply in enumerate(replies, start=1)]
    return "\n\n".join(parts)
