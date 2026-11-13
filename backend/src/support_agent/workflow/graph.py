"""The workflow graph (M5):

    intake -> classify -> route_after_classify -> investigate -> route_after_investigate
                                                                    -> END
                                                                    -> escalate
                                                -> needs_clarification  (placeholder)
                                                -> escalate

The model proposes a label; `route_after_classify` (plain code) decides where the run
goes. After the investigator, `route_after_investigate` sends any failure (LLM down,
invalid output, step limit) to escalation. Dependencies (the LLM client, the ownership
check, the tool registry and context) are passed in through `WorkflowDeps` and bound to
the nodes here, so no node ever builds its own client and tests can pass fakes.

No checkpointer yet: the graph runs start to finish in one call (pause/resume is M7).
"""

from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from support_agent.domain.enums import RunStatus
from support_agent.llm.client import LLMClient
from support_agent.tools.base import ToolContext, ToolRegistry
from support_agent.workflow.nodes.classify import OwnsOrder, classify
from support_agent.workflow.nodes.intake import intake
from support_agent.workflow.nodes.investigate import investigate
from support_agent.workflow.nodes.placeholders import escalate, needs_clarification
from support_agent.workflow.state import WorkflowState

Route = Literal["investigate", "needs_clarification", "escalate"]
AfterInvestigate = Literal["escalate", "end"]


@dataclass(frozen=True)
class WorkflowDeps:
    llm: LLMClient
    owns_order: OwnsOrder
    # Below this confidence the run asks the customer instead of guessing.
    confidence_threshold: float
    registry: ToolRegistry  # all read tools; the investigator cuts it down to its allowlist
    tool_context: ToolContext  # session, services and clock the tools run with
    max_tool_calls: int


def route_after_classify(state: WorkflowState, threshold: float) -> Route:
    """Plain-code routing on the classifier's output. The model never picks the route."""
    classification = state.get("classification")
    if state["errors"] or classification is None:
        return "escalate"
    if classification.category == "unclear" or classification.confidence < threshold:
        return "needs_clarification"
    return "investigate"


def route_after_investigate(state: WorkflowState) -> AfterInvestigate:
    """Plain-code routing: any recorded error means a human takes over."""
    return "escalate" if state["errors"] else "end"


def build_graph(deps: WorkflowDeps) -> CompiledStateGraph[WorkflowState]:
    async def classify_node(state: WorkflowState) -> dict[str, object]:
        return await classify(state, llm=deps.llm, owns_order=deps.owns_order)

    async def investigate_node(state: WorkflowState) -> dict[str, object]:
        return await investigate(
            state,
            llm=deps.llm,
            registry=deps.registry,
            ctx=deps.tool_context,
            max_tool_calls=deps.max_tool_calls,
        )

    def route(state: WorkflowState) -> Route:
        return route_after_classify(state, deps.confidence_threshold)

    graph = StateGraph(WorkflowState)
    graph.add_node("intake", intake)
    graph.add_node("classify", classify_node)
    graph.add_node("investigate", investigate_node)
    graph.add_node("needs_clarification", needs_clarification)
    graph.add_node("escalate", escalate)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "classify")
    graph.add_conditional_edges("classify", route)
    graph.add_conditional_edges(
        "investigate", route_after_investigate, {"escalate": "escalate", "end": END}
    )
    for end_node in ("needs_clarification", "escalate"):
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
    final = await build_graph(deps).ainvoke(state)
    return cast(WorkflowState, final)
