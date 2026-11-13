"""Resolve: draft, validate, critique, revise (agents/resolve.py), or the baseline's one draft.

Both modes build the same `ResolutionInputs` and get the same `ResolutionOutcome`. In
multi-agent mode `resolve()` runs the draft -> validator -> critic -> revision loop. In
baseline mode `baseline.decide()` makes one draft and runs the same validator, with no
critic. If the policy agent did not run (the supervisor skipped it for an order status
question), the findings are empty: no rule is cited, and a refund could not pass validation.

An LLM failure comes back inside the outcome as an escalation with `failure` set. The node
also records it as an error code, like the other agents' failures.
"""

from datetime import timedelta

from support_agent.agents import baseline
from support_agent.agents.policy import PolicyFindings
from support_agent.agents.resolution import ResolutionInputs
from support_agent.agents.resolve import resolve
from support_agent.domain.enums import RunMode
from support_agent.workflow.deps import WorkflowDeps
from support_agent.workflow.state import WorkflowError, WorkflowState

# Used when the policy agent did not run: nothing grounded, so nothing can be cited.
NO_FINDINGS = PolicyFindings(
    rules=[],
    dropped_rules=[],
    policy_versions=[],
    no_relevant_policy=True,
    conflicting_passages=False,
    ungrounded_citations=False,
)


async def resolve_case(state: WorkflowState, *, deps: WorkflowDeps) -> dict[str, object]:
    investigation = state.get("investigation")
    eligibility = state.get("eligibility")
    order_number = state.get("order_number")
    if (
        state["errors"]
        or investigation is None
        or investigation.evidence is None
        or eligibility is None
        or order_number is None
    ):  # the router makes this unreachable
        return {"errors": [WorkflowError.RESOLUTION_OUTPUT_INVALID]}

    policy = state.get("policy")
    inputs = ResolutionInputs(
        complaint_text=state["complaint_text"],
        order_number=order_number,
        evidence=investigation.evidence,
        findings=NO_FINDINGS if policy is None or policy.findings is None else policy.findings,
        eligibility=eligibility,
        delay_facts=state.get("delay_facts"),
    )
    settings = deps.tool_context.settings
    tolerance = timedelta(hours=settings.delivery_date_tolerance_hours)

    gathering = state.get("baseline_messages")
    if deps.mode == RunMode.BASELINE and gathering is not None:
        outcome = await baseline.decide(
            deps.llm,
            gathering,
            inputs,
            trace=investigation.trace,
            results=investigation.results,
            delivery_tolerance=tolerance,
        )
    else:
        outcome = await resolve(
            deps.llm,
            inputs,
            trace=investigation.trace,
            results=investigation.results,
            max_revisions=settings.resolution_max_revisions,
            delivery_tolerance=tolerance,
            critic_tier=settings.critic_tier,
        )
    update: dict[str, object] = {"outcome": outcome}
    if outcome.failure is not None:
        update["errors"] = [WorkflowError(outcome.failure.value)]
    return update
