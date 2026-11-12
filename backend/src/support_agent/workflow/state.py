"""The state that flows through the LangGraph workflow.

LangGraph keeps one state dict per run. Each node receives the whole state and returns
only the keys it changed; LangGraph merges them in. For most keys "merge" means
"replace". `errors` has a reducer (`operator.add`), so a node returns only its *new*
errors and they are appended to the list.

Later milestones add the remaining PRD fields (investigation plan, verified facts,
policy references, proposal id, ...).
"""

import operator
from enum import StrEnum
from typing import Annotated, NotRequired, TypedDict
from uuid import UUID

from support_agent.agents.classifier import Classification
from support_agent.domain.enums import RunStatus


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
    errors: Annotated[list[WorkflowError], operator.add]
