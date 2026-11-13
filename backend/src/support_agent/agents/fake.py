"""Scripted model answers for the investigator, for tests (see llm/fake.py).

The investigator makes three kinds of model calls. Build each one here:

    plan_reply()                      the structured plan (step 1)
    read_call("get_order", order, id) the model asks for a tool (step 2)
    text_reply("Done.")               the model stops calling tools (step 2)
    summary_reply([(fact, call_id)])  the structured evidence summary (step 3)

`simple_investigation()` is the shortest full run: one get_order call, one fact.

In the multi-agent workflow the investigator and the policy agent run in parallel, so their
calls reach the model in no fixed order. A single ordered script would hand one agent the
other's answers. `AgentFake` keeps one script per agent and picks the script from the request
(which tools it offers), so tests stay deterministic:

    AgentFake({"classifier": [...], "supervisor": [...], "investigator": [...],
               "policy": [...], "resolution": [...], "critic": [...]})

The baseline agent's loop uses the key "baseline" (its draft call uses "resolution").
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence

from support_agent.llm.errors import LLMError
from support_agent.llm.fake import ScriptStep, text_reply, tool_reply
from support_agent.llm.types import LLMRequest, ModelTier, ProviderReply


def plan_reply(
    goal: str = "Check the order.", steps: Sequence[str] = ("Look up the order.",)
) -> ProviderReply:
    return tool_reply("investigation_plan", {"goal": goal, "steps": list(steps)}, call_id="plan")


def read_call(tool: str, order_number: str, call_id: str) -> ProviderReply:
    return tool_reply(tool, {"order_number": order_number}, call_id=call_id)


def summary_reply(
    facts: Sequence[tuple[str, str]] = (),
    *,
    missing: Sequence[str] = (),
    plan_changes: Sequence[str] = (),
) -> ProviderReply:
    """`facts` are (statement, call_id) pairs."""
    return tool_reply(
        "evidence_summary",
        {
            "facts": [{"statement": text, "call_id": call_id} for text, call_id in facts],
            "missing_information": list(missing),
            "plan_changes": list(plan_changes),
        },
        call_id="summary",
    )


def search_call(query: str, order_number: str | None, call_id: str) -> ProviderReply:
    """The policy agent asks for a `search_policy` call."""
    args = {"query": query}
    if order_number is not None:
        args["order_number"] = order_number
    return tool_reply("search_policy", args, call_id=call_id)


def findings_reply(
    rules: Sequence[tuple[str, str]] = (),
    *,
    no_relevant_policy: bool = False,
    conflicting_passages: bool = False,
) -> ProviderReply:
    """The structured policy findings. `rules` are (statement, citation) pairs."""
    return tool_reply(
        "policy_findings_draft",
        {
            "rules": [{"statement": text, "citation": cite} for text, cite in rules],
            "no_relevant_policy": no_relevant_policy,
            "conflicting_passages": conflicting_passages,
        },
        call_id="findings",
    )


def simple_investigation(order_number: str = "ORD-1001") -> list[ScriptStep]:
    return [
        plan_reply(),
        read_call("get_order", order_number, "call_1"),
        text_reply("Done."),
        summary_reply([("The order exists.", "call_1")]),
    ]


def draft_reply(
    action: str = "refund",
    *,
    message: str = "Your return was received, so a refund is proposed and needs approval.",
    call_ids: Sequence[str] = ("call_1",),
    citations: Sequence[str] = (),
    rationale: str = "The evidence supports it.",
    **extra: object,
) -> ProviderReply:
    """The resolution agent's structured draft. `extra` adds fields the schema does not
    have, such as a model-supplied `amount_minor`, which must be ignored."""
    return tool_reply(
        "resolution_draft",
        {
            "action": action,
            "rationale": rationale,
            "cited_call_ids": list(call_ids),
            "policy_citations": list(citations),
            "customer_message": message,
            **extra,
        },
        call_id="draft",
    )


def critique_reply(verdict: str = "approve", issues: Sequence[str] = ()) -> ProviderReply:
    """The critic's structured verdict."""
    return tool_reply(
        "critic_verdict", {"verdict": verdict, "issues": list(issues)}, call_id="critic"
    )


def supervisor_reply(
    *,
    investigator: bool = True,
    policy: bool = True,
    investigator_focus: str = "",
    policy_focus: str = "",
) -> ProviderReply:
    """The supervisor's structured decision."""
    return tool_reply(
        "supervisor_decision",
        {
            "run_investigator": investigator,
            "run_policy": policy,
            "investigator_focus": investigator_focus,
            "policy_focus": policy_focus,
        },
        call_id="supervisor",
    )


def policy_run(
    citation: str, statement: str = "Returns are accepted within 30 days.", order: str = "ORD-1001"
) -> list[ScriptStep]:
    """The shortest full policy agent run: one search, then findings with one rule."""
    return [
        search_call("return window", order, "search_1"),
        text_reply("Done."),
        findings_reply([(statement, citation)]),
    ]


_FORCED_TOOL_AGENT = {
    "classification": "classifier",
    "supervisor_decision": "supervisor",
    "investigation_plan": "investigator",
    "evidence_summary": "investigator",
    "policy_findings_draft": "policy",
    "resolution_draft": "resolution",
    "critic_verdict": "critic",
}


def agent_of(request: LLMRequest) -> str:
    """Which agent made this request, judged by the tools it offers."""
    if request.forced_tool in _FORCED_TOOL_AGENT:
        return _FORCED_TOOL_AGENT[request.forced_tool]
    names = {tool.name for tool in request.tools}
    if "search_policy" in names and "get_order" in names:
        return "baseline"
    return "policy" if "search_policy" in names else "investigator"


class AgentFake:
    """A scripted provider with one script per agent. See the module docstring."""

    def __init__(self, scripts: Mapping[str, Sequence[ScriptStep]]) -> None:
        self._scripts = {agent: list(steps) for agent, steps in scripts.items()}
        self.requests: list[LLMRequest] = []
        self.requests_by_agent: defaultdict[str, list[LLMRequest]] = defaultdict(list)

    @property
    def name(self) -> str:
        return "fake"

    def model_for(self, tier: ModelTier) -> str:
        return f"fake-{tier}"

    async def generate(self, request: LLMRequest) -> ProviderReply:
        agent = agent_of(request)
        self.requests.append(request)
        self.requests_by_agent[agent].append(request)
        script = self._scripts.get(agent)
        if not script:
            raise LLMError(f"AgentFake: no scripted step left for the {agent!r} agent.")
        step = script.pop(0)
        if isinstance(step, LLMError):
            raise step
        return step
