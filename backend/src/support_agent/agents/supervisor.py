"""The supervisor: after classification, choose which specialists gather information.

One structured call on the small tier (`SUPERVISOR_TIER`). The model says which of the
investigator and the policy agent to run and gives each a short focus hint. Code then:

- enforces a minimum set per category (`enforce_minimum`): refund and damaged_item need both,
  order_status needs the investigator. The model can add a specialist, never remove one;
- keeps the hints as plain text. They are model output, so whoever uses them wraps them as
  untrusted data (agents/investigator.py, agents/policy.py).

The customer's long-term memory (rendered by code, ADR-013) is shown as untrusted
background, like the complaint. It can only make the model add a specialist, never remove a
required one.

The supervisor only affects information gathering. It never sees or sets eligibility, an
amount or the final action: those come from code, the resolution agent and a human.
"""

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from support_agent.agents.history import render_history
from support_agent.domain.enums import TicketCategory
from support_agent.llm.client import LLMClient
from support_agent.llm.tool_loop import wrap_untrusted
from support_agent.llm.types import ModelTier, system_message, user_message

PROMPT_PATH = Path(__file__).parent / "prompts" / "supervisor.md"

INVESTIGATOR = "investigator"
POLICY = "policy"

# Specialists that must run, whatever the model chose (PRD FR-7, FR-8).
REQUIRED_SPECIALISTS: dict[TicketCategory, frozenset[str]] = {
    TicketCategory.REFUND: frozenset({INVESTIGATOR, POLICY}),
    TicketCategory.DAMAGED_ITEM: frozenset({INVESTIGATOR, POLICY}),
    TicketCategory.ORDER_STATUS: frozenset({INVESTIGATOR}),
}

Hint = Annotated[str, StringConstraints(max_length=200)]


class SupervisorDecision(BaseModel):
    """What the model must return. Not trusted until `enforce_minimum` has checked it."""

    run_investigator: bool = Field(description="Run the investigator (order, return, shipment).")
    run_policy: bool = Field(description="Run the policy agent (searches the store policies).")
    investigator_focus: Hint = Field(default="", description="One short sentence, or empty.")
    policy_focus: Hint = Field(default="", description="One short sentence, or empty.")


@dataclass(frozen=True)
class SupervisorPlan:
    """The decision after code enforced the minimum set."""

    run_investigator: bool
    run_policy: bool
    # Model-written: keep out of logs and audit events.
    investigator_focus: str
    policy_focus: str
    # Specialists the model left out but code added, for the audit trail.
    added_by_code: list[str] = field(default_factory=list)


@cache
def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def enforce_minimum(category: TicketCategory, decision: SupervisorDecision) -> SupervisorPlan:
    """The model's choice, plus every specialist this category requires."""
    required = REQUIRED_SPECIALISTS[category]
    chosen = {INVESTIGATOR: decision.run_investigator, POLICY: decision.run_policy}
    added = [name for name, picked in chosen.items() if name in required and not picked]
    return SupervisorPlan(
        run_investigator=decision.run_investigator or INVESTIGATOR in required,
        run_policy=decision.run_policy or POLICY in required,
        investigator_focus=decision.investigator_focus.strip(),
        policy_focus=decision.policy_focus.strip(),
        added_by_code=added,
    )


async def supervise(
    llm: LLMClient,
    *,
    complaint_text: str,
    category: TicketCategory,
    order_number: str,
    tier: ModelTier,
    customer_memory: str = "",
) -> SupervisorPlan:
    """May raise LLMUnavailableError or StructuredOutputError (the workflow node handles them)."""
    text = (
        f"Complaint category: {category.value}\nOrder number: {order_number}\n\n"
        + wrap_untrusted("customer_complaint", complaint_text)
    )
    if customer_memory:  # background only, wrapped as untrusted data
        text += "\n\n" + render_history(customer_memory)
    decision = await llm.structured(
        [system_message(load_prompt()), user_message(text)],
        SupervisorDecision,
        tier=tier,
    )
    return enforce_minimum(category, decision)
