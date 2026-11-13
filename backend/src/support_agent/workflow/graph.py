"""The workflow graph (M6).

Multi-agent mode:

    intake -> classify -> route_after_classify
        -> supervise -> route_after_supervise --+-> investigate ---+
                                                +-> policy --------+   (in parallel)
                                                +-> escalate       |
        needs_clarification / ask_for_order / escalate              v
                                                                 gather -> route_after_gather
                  -> reinvestigate (the one extra round) -> gather      -> resolve_case
                                                                        -> escalate
        resolve_case -> route_after_resolve -> propose_refund | explain | request_info | escalate

Baseline mode replaces supervise/investigate/policy with one `baseline_agent` node and has
no extra round. Everything after `gather` is shared, so both modes meet the same code.

Plain-code routers decide every route: the model proposes, code decides. The supervisor
only chooses which specialists gather information. Money only moves after a human approval,
and not in this graph: `propose_refund` stores a proposal and a pending approval.

Dependencies (the LLM client, services and sessions) come in through `WorkflowDeps` and are
bound to the nodes here, so no node builds its own and tests can pass fakes.

No checkpointer yet: the graph runs start to finish in one call (pause/resume is M7).
"""

from collections.abc import Awaitable, Callable, Hashable
from typing import Any, Protocol, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from support_agent.domain.enums import ResolutionAction, RunMode, RunStatus
from support_agent.workflow.deps import WorkflowDeps
from support_agent.workflow.nodes import finish, gather, resolve, specialists, supervise
from support_agent.workflow.nodes.classify import classify
from support_agent.workflow.nodes.intake import intake
from support_agent.workflow.nodes.placeholders import ask_for_order, needs_clarification
from support_agent.workflow.state import WorkflowState

__all__ = ["WorkflowDeps", "build_graph", "initial_state", "run_workflow"]


class NodeFn(Protocol):
    """A node as LangGraph wants it: the first parameter is named `state`."""

    def __call__(self, state: WorkflowState) -> Awaitable[dict[str, object]]: ...


def _targets(*names: str) -> dict[Hashable, str]:
    """A route-name -> node-name map where each route is named after its node."""
    return {name: name for name in names}


# What each resolution action leads to.
_NODE_FOR_ACTION = {
    ResolutionAction.REFUND: "propose_refund",
    ResolutionAction.EXPLAIN_STATUS: "explain",
    ResolutionAction.REQUEST_INFO: "request_info",
    ResolutionAction.ESCALATE: "escalate",
}


def route_after_classify(
    state: WorkflowState, threshold: float, mode: RunMode = RunMode.MULTI_AGENT
) -> str:
    """Plain-code routing on the classifier's output. The model never picks the route."""
    classification = state.get("classification")
    if state["errors"] or classification is None:
        return "escalate"
    if classification.category == "unclear" or classification.confidence < threshold:
        return "needs_clarification"
    if state.get("order_number") is None:
        return "ask_for_order"  # no verified order to work on
    return "baseline_agent" if mode == RunMode.BASELINE else "supervise"


def route_after_supervise(state: WorkflowState) -> list[str]:
    """The specialists to run now. Returning several runs them in parallel."""
    plan = state.get("supervisor")
    if state["errors"] or plan is None:
        return ["escalate"]
    branches = []
    if plan.run_investigator:
        branches.append("investigate")
    if plan.run_policy:
        branches.append("policy")
    return branches


def route_after_gather(state: WorkflowState) -> str:
    if state["errors"]:
        return "escalate"
    return "reinvestigate" if state.get("retry_investigation") else "resolve_case"


def route_after_resolve(state: WorkflowState) -> str:
    """By the validated outcome. Any recorded error, or no outcome, goes to a person."""
    outcome = state.get("outcome")
    if state["errors"] or outcome is None:
        return "escalate"
    return _NODE_FOR_ACTION[outcome.action]


def build_graph(deps: WorkflowDeps) -> CompiledStateGraph[WorkflowState]:
    baseline = deps.mode == RunMode.BASELINE

    async def classify_node(state: WorkflowState) -> dict[str, object]:
        return await classify(state, llm=deps.llm, owns_order=deps.owns_order)

    def bind(node: Callable[..., Awaitable[dict[str, object]]]) -> NodeFn:
        """Give a node its dependencies. A plain closure keeps LangGraph's typing simple."""

        async def run_node(state: WorkflowState) -> dict[str, object]:
            return await node(state, deps=deps)

        return run_node

    def route(state: WorkflowState) -> str:
        return route_after_classify(state, deps.confidence_threshold, deps.mode)

    graph = StateGraph(WorkflowState)
    graph.add_node("intake", intake)
    graph.add_node("classify", classify_node)
    graph.add_node("needs_clarification", needs_clarification)
    graph.add_node("ask_for_order", ask_for_order)
    graph.add_node("gather", bind(gather.gather))
    graph.add_node("resolve_case", bind(resolve.resolve_case))
    graph.add_node("propose_refund", bind(finish.propose_refund))
    graph.add_node("explain", finish.explain)
    graph.add_node("request_info", finish.request_info)
    graph.add_node("escalate", finish.escalate)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "classify")

    gathering_entry = "baseline_agent" if baseline else "supervise"
    graph.add_conditional_edges(
        "classify",
        route,
        _targets("needs_clarification", "ask_for_order", "escalate", gathering_entry),
    )

    if baseline:
        graph.add_node("baseline_agent", bind(specialists.baseline_agent))
        graph.add_edge("baseline_agent", "gather")
    else:
        graph.add_node("supervise", bind(supervise.supervise))
        graph.add_node("investigate", bind(specialists.investigate))
        graph.add_node("policy", bind(specialists.policy))
        graph.add_node("reinvestigate", bind(specialists.reinvestigate))
        graph.add_conditional_edges(
            "supervise", route_after_supervise, _targets("investigate", "policy", "escalate")
        )
        # Both branches end in `gather`. They run in the same step, so `gather` runs once,
        # after both have finished (or after the only one that ran).
        graph.add_edge("investigate", "gather")
        graph.add_edge("policy", "gather")
        graph.add_edge("reinvestigate", "gather")

    after_gather = ["resolve_case", "escalate"] + ([] if baseline else ["reinvestigate"])
    graph.add_conditional_edges("gather", route_after_gather, _targets(*after_gather))
    graph.add_conditional_edges(
        "resolve_case", route_after_resolve, _targets(*_NODE_FOR_ACTION.values(), "escalate")
    )

    for end_node in (
        "needs_clarification",
        "ask_for_order",
        "propose_refund",
        "explain",
        "request_info",
        "escalate",
    ):
        graph.add_edge(end_node, END)
    return graph.compile()


def initial_state(
    *,
    run_id: UUID,
    ticket_id: UUID,
    customer_id: UUID,
    complaint_text: str,
    ticket_order_number: str | None,
) -> WorkflowState:
    return WorkflowState(
        run_id=run_id,
        ticket_id=ticket_id,
        customer_id=customer_id,
        complaint_text=complaint_text,
        ticket_order_number=ticket_order_number,
        status=RunStatus.CREATED,
        errors=[],
    )


async def run_workflow(deps: WorkflowDeps, state: WorkflowState) -> WorkflowState:
    """Run the graph to the end and return the final state."""
    final: Any = await build_graph(deps).ainvoke(state)
    return cast(WorkflowState, final)
