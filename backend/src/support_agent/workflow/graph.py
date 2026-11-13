"""The workflow graph (M6, with M7's pauses).

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

Pauses (M7, nodes/pauses.py), saved by the checkpointer so a later call can resume them:

        propose_refund -> wait_for_approval (pause) -> check_approval
            -> execute_refund | reopen_ticket | escalate              (nodes/approval.py)
        needs_clarification / ask_for_order / request_info -> route_to_customer
            -> wait_for_customer (pause) -> classify (unclear, no order) | gathering (more info)
            -> escalate, once the clarification limit is reached

Baseline mode replaces supervise/investigate/policy with one `baseline_agent` node and has
no extra round. Everything after `gather` is shared, so both modes meet the same code.

Plain-code routers decide every route: the model proposes, code decides. The supervisor
only chooses which specialists gather information. Money only moves in `execute_refund`,
after a reviewer's approval that code reads back from the database.

Dependencies (the LLM client, services and sessions) come in through `WorkflowDeps` and are
bound to the nodes here, so no node builds its own and tests can pass fakes.

Each call runs one segment: from the start (or a resume) to the next pause or the end.
`start_workflow` and `resume_workflow` return what the segment did, so the caller can record
exactly that part (services/run_recording.py).
"""

from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from support_agent.domain.enums import ApprovalDecision, ResolutionAction, RunMode, RunStatus
from support_agent.workflow.deps import WorkflowDeps
from support_agent.workflow.nodes import (
    approval,
    finish,
    gather,
    pauses,
    resolve,
    specialists,
    supervise,
)
from support_agent.workflow.nodes.classify import classify
from support_agent.workflow.nodes.intake import intake
from support_agent.workflow.nodes.placeholders import ask_for_order, needs_clarification
from support_agent.workflow.state import AskedFor, WorkflowState, restore_enums

__all__ = [
    "Checkpointer",
    "NotPausedError",
    "Segment",
    "WorkflowDeps",
    "build_graph",
    "initial_state",
    "resume_workflow",
    "start_workflow",
]

Checkpointer = BaseCheckpointSaver[Any]


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


def route_after_propose(state: WorkflowState) -> str:
    """A stored proposal waits for the reviewer; a refused one has already escalated."""
    return "wait_for_approval" if "proposal_id" in state else "end"


def route_after_approval(state: WorkflowState) -> str:
    """By the decision stored in the database. Anything but approve or reject escalates."""
    decision = state.get("approval_decision")
    if decision == ApprovalDecision.APPROVED:
        return "execute_refund"
    if decision == ApprovalDecision.REJECTED:
        return "reopen_ticket"
    return "escalate"  # expired, still pending, or no approval at all


def route_to_customer(state: WorkflowState, max_rounds: int) -> str:
    """Ask, unless the customer has already answered as many questions as allowed."""
    if state.get("clarification_rounds", 0) >= max_rounds:
        return "escalate"
    return "wait_for_customer"


def route_after_customer(state: WorkflowState, gathering_entry: str) -> str:
    """Continue from the step that needed the answer."""
    if state.get("asked_for") == AskedFor.MORE_INFORMATION:
        return gathering_entry  # the case was understood: gather again with the new detail
    return "classify"  # the category or the order was unclear: classify the whole thread


def build_graph(
    deps: WorkflowDeps, checkpointer: Checkpointer
) -> CompiledStateGraph[WorkflowState]:
    baseline = deps.mode == RunMode.BASELINE
    gathering_entry = "baseline_agent" if baseline else "supervise"

    async def classify_node(state: WorkflowState) -> dict[str, object]:
        return await classify(state, llm=deps.llm, owns_order=deps.owns_order)

    def bind(node: Callable[..., Awaitable[dict[str, object]]]) -> NodeFn:
        """Give a node its dependencies. A plain closure keeps LangGraph's typing simple."""

        async def run_node(state: WorkflowState) -> dict[str, object]:
            return await node(state, deps=deps)

        return run_node

    def route(state: WorkflowState) -> str:
        return route_after_classify(state, deps.confidence_threshold, deps.mode)

    def ask_or_escalate(state: WorkflowState) -> str:
        return route_to_customer(state, deps.max_clarification_rounds)

    def continue_after_reply(state: WorkflowState) -> str:
        return route_after_customer(state, gathering_entry)

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
    graph.add_node("wait_for_approval", pauses.wait_for_approval)
    graph.add_node("check_approval", bind(approval.check_approval))
    graph.add_node("execute_refund", bind(approval.execute_refund))
    graph.add_node("reopen_ticket", approval.reopen_ticket)
    graph.add_node("wait_for_customer", pauses.wait_for_customer)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "classify")

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

    # Approval: pause, then fixed code on the decision stored in the database.
    graph.add_conditional_edges(
        "propose_refund",
        route_after_propose,
        {"wait_for_approval": "wait_for_approval", "end": END},
    )
    graph.add_edge("wait_for_approval", "check_approval")
    graph.add_conditional_edges(
        "check_approval",
        route_after_approval,
        _targets("execute_refund", "reopen_ticket", "escalate"),
    )

    # Questions for the customer: pause, then continue from the step that needed the answer.
    for ask_node in ("needs_clarification", "ask_for_order", "request_info"):
        graph.add_conditional_edges(
            ask_node, ask_or_escalate, _targets("wait_for_customer", "escalate")
        )
    graph.add_conditional_edges(
        "wait_for_customer", continue_after_reply, _targets("classify", gathering_entry)
    )

    for end_node in ("explain", "escalate", "execute_refund", "reopen_ticket"):
        graph.add_edge(end_node, END)
    return graph.compile(checkpointer=checkpointer)


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
        customer_replies=[],
        errors=[],
    )


class NotPausedError(Exception):
    """The thread has no pending pause to resume (finished, never started, or unknown)."""


@dataclass(frozen=True)
class Segment:
    """One call's worth of a run: from the start or a resume to a pause or the end."""

    state: WorkflowState  # the whole state afterwards
    # Only what nodes wrote during this segment (reducer keys: only the new items). Keys are
    # missing for steps that did not run, which is how recording avoids repeating events.
    written: WorkflowState
    paused: bool  # True: waiting at an interrupt, resumable later


def thread_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


async def start_workflow(
    deps: WorkflowDeps, checkpointer: Checkpointer, thread_id: str, state: WorkflowState
) -> Segment:
    return await _run_segment(build_graph(deps, checkpointer), state, thread_id)


async def resume_workflow(
    deps: WorkflowDeps, checkpointer: Checkpointer, thread_id: str, value: dict[str, str]
) -> Segment:
    """Continue a paused run. A fresh graph is fine: everything comes from the checkpoint."""
    graph = build_graph(deps, checkpointer)
    snapshot = await graph.aget_state(thread_config(thread_id))
    if not snapshot.interrupts:
        raise NotPausedError(thread_id)
    return await _run_segment(graph, Command(resume=value), thread_id)


_REDUCER_KEYS = ("errors", "customer_replies")


async def _run_segment(
    graph: CompiledStateGraph[WorkflowState], graph_input: Any, thread_id: str
) -> Segment:
    config = thread_config(thread_id)
    written: dict[str, Any] = {}
    # durability="sync": each step's checkpoint is saved before the next step starts, so a
    # pause is on disk before we commit our own rows and answer the request.
    async for chunk in graph.astream(graph_input, config, stream_mode="updates", durability="sync"):
        for node, update in chunk.items():
            if node == "__interrupt__" or not update:
                continue
            for key, value in update.items():
                if key in _REDUCER_KEYS:
                    written.setdefault(key, []).extend(value)
                else:
                    written[key] = value
    snapshot = await graph.aget_state(config)
    return Segment(
        state=restore_enums(snapshot.values),
        written=cast(WorkflowState, written),
        paused=bool(snapshot.interrupts),
    )
