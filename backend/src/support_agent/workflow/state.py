"""The state that flows through the LangGraph workflow.

LangGraph keeps one state dict per run. Each node receives the whole state and returns
only the keys it changed; LangGraph merges them in. For most keys "merge" means
"replace". `errors` has a reducer (`operator.add`), so a node returns only its *new*
errors and they are appended to the list.

Later milestones add the remaining PRD fields (policy references, proposal id, ...).
"""

import operator
from enum import StrEnum
from typing import Annotated, NotRequired, TypedDict
from uuid import UUID

from support_agent.agents.classifier import Classification
from support_agent.agents.investigator import Evidence, InvestigationPlan
from support_agent.domain.enums import RunStatus
from support_agent.llm.tool_loop import ToolCallRecord


class OrderHint(StrEnum):
    """What happened to the order number the classifier extracted (it is only a hint)."""

    NONE = "none"  # the classifier found no order number
    MATCHED_TICKET = "matched_ticket"  # same order the ticket already names
    VERIFIED = "verified"  # ticket names no order; this customer owns the hinted one
    IGNORED_NOT_OWNED = "ignored_not_owned"  # unknown, or another customer's order
    IGNORED_CONFLICTS_TICKET = "ignored_conflicts_ticket"  # ticket names a different order


class WorkflowError(StrEnum):
    """Error codes a node can record. Codes only: never model output or customer text."""

    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"  # every provider failed (LLMUnavailableError)
    CLASSIFIER_OUTPUT_INVALID = "CLASSIFIER_OUTPUT_INVALID"  # StructuredOutputError
    INVESTIGATOR_OUTPUT_INVALID = "INVESTIGATOR_OUTPUT_INVALID"  # plan, tool call or summary
    INVESTIGATION_STEP_LIMIT = "INVESTIGATION_STEP_LIMIT"  # the model wanted too many tool calls


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
    # Set by the investigate node. `tool_trace` is present (even if empty) exactly when the
    # investigator ran, which is how services/runs.py knows to write investigation events.
    investigation_plan: NotRequired[InvestigationPlan | None]
    evidence: NotRequired[Evidence | None]  # None if the investigation failed
    tool_trace: NotRequired[list[ToolCallRecord]]
    missing_information: NotRequired[list[str]]  # what we must ask the customer for
    errors: Annotated[list[WorkflowError], operator.add]
