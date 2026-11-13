"""Gather: the join after the specialists. Required evidence, then the facts code computes.

Runs once after the parallel branches (and again after the one extra investigation round):

1. Any recorded error means a specialist failed: do nothing here, the router escalates.
2. If the extra round ran, merge it into the first investigation.
3. Required evidence (agents/required_evidence.py): code fetches `get_order` and, where it
   matters, `get_shipment`, if the investigator did not.
4. If a required lookup is still missing and no extra round has run yet (multi-agent mode
   only), ask for exactly one more investigation round. Code decides this, not the model.
5. Eligibility and the shipping-delay facts, computed by code from the database.

Nothing here calls a model.
"""

from dataclasses import replace

from support_agent.agents.investigator import Evidence, InvestigationOutcome
from support_agent.agents.required_evidence import (
    build_delay_facts,
    ensure_required_evidence,
    missing_required_tools,
)
from support_agent.core.errors import AppError
from support_agent.domain.enums import RunMode, TicketCategory
from support_agent.llm.tool_loop import ToolCallRecord, ToolResult
from support_agent.workflow.deps import WorkflowDeps
from support_agent.workflow.state import WorkflowError, WorkflowState

ROUND_TWO_PREFIX = "r2_"  # keeps the second round's call ids from clashing with the first's


def _prefixed(outcome: InvestigationOutcome) -> InvestigationOutcome:
    """The same outcome with every call id renamed, so two rounds can share one trace."""

    def rename(call_id: str) -> str:
        return ROUND_TWO_PREFIX + call_id

    evidence = outcome.evidence
    if evidence is not None:
        evidence = evidence.model_copy(
            update={
                "facts": [
                    f.model_copy(update={"call_id": rename(f.call_id)}) for f in evidence.facts
                ],
                "dropped_facts": [
                    f.model_copy(update={"call_id": rename(f.call_id)})
                    for f in evidence.dropped_facts
                ],
            }
        )
    return replace(
        outcome,
        trace=[r.model_copy(update={"call_id": rename(r.call_id)}) for r in outcome.trace],
        results=[replace(r, call_id=rename(r.call_id)) for r in outcome.results],
        evidence=evidence,
    )


def merge_rounds(first: InvestigationOutcome, second: InvestigationOutcome) -> InvestigationOutcome:
    """Both rounds' calls and facts. The second round's missing_information replaces the first's."""
    later = _prefixed(second)
    trace: list[ToolCallRecord] = [*first.trace, *later.trace]
    results: list[ToolResult] = [*first.results, *later.results]
    evidence: Evidence | None = first.evidence
    if first.evidence is not None and later.evidence is not None:
        evidence = Evidence(
            facts=[*first.evidence.facts, *later.evidence.facts],
            dropped_facts=[*first.evidence.dropped_facts, *later.evidence.dropped_facts],
            missing_information=later.evidence.missing_information,
            plan_changes=[*first.evidence.plan_changes, *later.evidence.plan_changes],
        )
    return replace(first, trace=trace, results=results, evidence=evidence)


async def gather(state: WorkflowState, *, deps: WorkflowDeps) -> dict[str, object]:
    investigation = state.get("investigation")
    classification = state.get("classification")
    order_number = state.get("order_number")
    if state["errors"] or investigation is None or classification is None or order_number is None:
        return {"retry_investigation": False}

    category = TicketCategory(classification.category)
    retry = state.get("investigation_retry")
    if retry is not None:
        investigation = merge_rounds(investigation, retry)

    ctx = replace(deps.tool_context, customer_id=state["customer_id"])
    investigation = await ensure_required_evidence(
        deps.registry, ctx, investigation, category=category, order_number=order_number
    )

    gaps = missing_required_tools(investigation.results, category)
    extra_round_allowed = deps.mode == RunMode.MULTI_AGENT and retry is None
    if gaps and extra_round_allowed:
        return {"investigation": investigation, "retry_investigation": True, "retry_gaps": gaps}

    now = ctx.clock()
    try:
        facts = await deps.load_case_facts(state["customer_id"], order_number, now)
    except AppError:
        return {
            "investigation": investigation,
            "retry_investigation": False,
            "errors": [WorkflowError.CASE_FACTS_UNAVAILABLE],
        }
    return {
        "investigation": investigation,
        "retry_investigation": False,
        "eligibility": facts.eligibility,
        "delay_facts": build_delay_facts(
            investigation.results, now, facts.investigation_threshold_days
        ),
    }
