"""The gathering branches: the investigator, the policy agent and the single-agent baseline.

The investigator and the policy agent run in parallel (graph.py). Each opens its own
short-lived database session (`deps.branch_sessions`), because one AsyncSession must not
be used by two tasks at once, and writes only its own state key:

    investigate    -> `investigation`        (plus `errors`)
    policy         -> `policy`               (plus `errors`)
    reinvestigate  -> `investigation_retry`  (plus `errors`)   the one extra round
    baseline_agent -> `investigation`, `policy`, `baseline_messages`   baseline mode only

The customer id for the tools is the state's (the ticket's owner, matched to the JWT when
the run started), never the model's. A failure never raises: it becomes an error code and
the router sends the run to escalation. The trace is kept either way.
"""

from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agents import baseline
from support_agent.agents.investigator import (
    Evidence,
    InvestigationFailure,
    InvestigationOutcome,
)
from support_agent.agents.investigator import investigate as run_investigator
from support_agent.agents.policy import PolicyOutcome, research_policy
from support_agent.tools.base import ToolContext
from support_agent.workflow.deps import WorkflowDeps
from support_agent.workflow.state import WorkflowError, WorkflowState, customer_text


def _branch_context(state: WorkflowState, deps: WorkflowDeps, session: AsyncSession) -> ToolContext:
    # `session` is the branch's own session; the customer is the state's, not the context's.
    return replace(deps.tool_context, session=session, customer_id=state["customer_id"])


# The router never sends a run here without a label and a verified order. If it ever did,
# the run would escalate with this code instead of crashing.
_NO_CASE = [WorkflowError.INVESTIGATOR_OUTPUT_INVALID]


async def _investigate(
    state: WorkflowState, deps: WorkflowDeps, order_number: str, focus: str | None
) -> InvestigationOutcome:
    async with deps.branch_sessions() as session:
        return await run_investigator(
            deps.llm,
            deps.registry,
            _branch_context(state, deps, session),
            complaint_text=customer_text(state),
            order_number=order_number,
            max_tool_calls=deps.max_tool_calls,
            focus=focus or None,
        )


def _failure_update(outcome: InvestigationOutcome) -> list[WorkflowError]:
    failure: InvestigationFailure | None = outcome.failure
    return [] if failure is None else [WorkflowError(failure.value)]


async def investigate(state: WorkflowState, *, deps: WorkflowDeps) -> dict[str, object]:
    order_number = state.get("order_number")
    if order_number is None:
        return {"errors": _NO_CASE}
    outcome = await _investigate(state, deps, order_number, state["supervisor"].investigator_focus)
    update: dict[str, object] = {"investigation": outcome}
    if errors := _failure_update(outcome):
        update["errors"] = errors
    return update


async def reinvestigate(state: WorkflowState, *, deps: WorkflowDeps) -> dict[str, object]:
    """The one extra round: the same investigator, told which records are still missing."""
    order_number = state.get("order_number")
    if order_number is None:
        return {"errors": _NO_CASE}
    gaps = ", ".join(state.get("retry_gaps", []))
    # Tool names written by code, so the note is safe to send as part of the hint.
    base = state["supervisor"].investigator_focus
    focus = f"{base} Second pass: still missing a successful {gaps} result."
    outcome = await _investigate(state, deps, order_number, focus.strip())
    update: dict[str, object] = {"investigation_retry": outcome}
    if errors := _failure_update(outcome):
        update["errors"] = errors
    return update


async def policy(state: WorkflowState, *, deps: WorkflowDeps) -> dict[str, object]:
    classification = state.get("classification")
    order_number = state.get("order_number")
    if classification is None or order_number is None:
        return {"errors": [WorkflowError.POLICY_OUTPUT_INVALID]}
    settings = deps.tool_context.settings
    async with deps.branch_sessions() as session:
        outcome = await research_policy(
            deps.llm,
            deps.registry,
            _branch_context(state, deps, session),
            complaint_text=customer_text(state),
            category=classification.category,
            order_number=order_number,
            max_searches=settings.policy_agent_max_searches,
            search_tier=settings.policy_agent_search_tier,
            findings_tier=settings.policy_agent_findings_tier,
            focus=state["supervisor"].policy_focus or None,
        )
    update: dict[str, object] = {"policy": outcome}
    if outcome.failure is not None:
        update["errors"] = [WorkflowError(outcome.failure.value)]
    return update


async def baseline_agent(state: WorkflowState, *, deps: WorkflowDeps) -> dict[str, object]:
    """Baseline mode: one loop with every read tool plus search_policy."""
    classification = state.get("classification")
    order_number = state.get("order_number")
    if classification is None or order_number is None:
        return {"errors": [WorkflowError.BASELINE_OUTPUT_INVALID]}
    settings = deps.tool_context.settings
    gathering = await baseline.gather(
        deps.llm,
        deps.registry,
        replace(deps.tool_context, customer_id=state["customer_id"]),
        complaint_text=customer_text(state),
        category=classification.category,
        order_number=order_number,
        max_tool_calls=deps.max_tool_calls + settings.policy_agent_max_searches,
    )
    # Shaped like the multi-agent outputs, so the shared steps after this need no special case.
    investigation = InvestigationOutcome(
        plan=None,
        trace=gathering.trace,
        evidence=Evidence(facts=[], dropped_facts=[], missing_information=[], plan_changes=[]),
        failure=None,
        results=gathering.results,
    )
    findings = baseline.grounded_findings(gathering.results)
    update: dict[str, object] = {
        "investigation": investigation,
        "policy": PolicyOutcome(trace=[], searches=[], findings=findings, failure=None),
        "baseline_messages": gathering,
    }
    if gathering.failure is not None:
        update["errors"] = [WorkflowError(gathering.failure.value)]
    return update
