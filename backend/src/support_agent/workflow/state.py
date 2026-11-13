"""The state that flows through the LangGraph workflow.

LangGraph keeps one state dict per run. Each node receives the whole state and returns
only the keys it changed; LangGraph merges them in. For most keys "merge" means
"replace". `errors` has a reducer (`operator.add`), so a node returns only its *new*
errors and they are appended to the list.

The two specialists run in parallel (graph.py). They never write the same key: the
investigator writes `investigation`, the policy agent writes `policy`, and both may append
to `errors`, the one key with a reducer. That is what makes the parallel merge safe.

Nothing here is stored as is. services/runs.py turns the final state into audit events
(ids, counts and codes only) and rows. Several values hold customer data or model text
(evidence, findings, the outcome's message): they stay in memory.
"""

import operator
from enum import StrEnum
from typing import Annotated, NotRequired, TypedDict
from uuid import UUID

from support_agent.agents.baseline import BaselineGathering
from support_agent.agents.classifier import Classification
from support_agent.agents.investigator import InvestigationOutcome
from support_agent.agents.policy import PolicyOutcome
from support_agent.agents.resolve import ResolutionOutcome
from support_agent.agents.supervisor import SupervisorPlan
from support_agent.domain.eligibility import EligibilityDecision
from support_agent.domain.enums import RunStatus
from support_agent.domain.shipping import ShippingDelayFacts


class OrderHint(StrEnum):
    """What happened to the order number the classifier extracted (it is only a hint)."""

    NONE = "none"  # the classifier found no order number
    MATCHED_TICKET = "matched_ticket"  # same order the ticket already names
    VERIFIED = "verified"  # ticket names no order; this customer owns the hinted one
    IGNORED_NOT_OWNED = "ignored_not_owned"  # unknown, or another customer's order
    IGNORED_CONFLICTS_TICKET = "ignored_conflicts_ticket"  # ticket names a different order


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
    investigation: NotRequired[InvestigationOutcome]  # after the required-evidence step
    investigation_retry: NotRequired[InvestigationOutcome]  # the one extra round, if any
    retry_gaps: NotRequired[list[str]]  # the required lookups the extra round is looking for
    retry_investigation: NotRequired[bool]  # set by the gather node, read by the router
    policy: NotRequired[PolicyOutcome]
    baseline_messages: NotRequired[BaselineGathering]  # baseline mode only

    # Computed by code, never by a model.
    eligibility: NotRequired[EligibilityDecision]
    delay_facts: NotRequired[ShippingDelayFacts | None]

    # Resolution and what followed.
    outcome: NotRequired[ResolutionOutcome]
    proposal_id: NotRequired[UUID]
    customer_reply: NotRequired[str]  # saved on the ticket: only verified outcomes
    proposal_refusal: NotRequired[str]  # the error code, if code refused the proposal
    errors: Annotated[list[WorkflowError], operator.add]
