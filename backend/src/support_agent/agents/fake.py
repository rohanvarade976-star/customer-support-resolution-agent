"""Scripted model answers for the investigator, for tests (see llm/fake.py).

The investigator makes three kinds of model calls. Build each one here:

    plan_reply()                      the structured plan (step 1)
    read_call("get_order", order, id) the model asks for a tool (step 2)
    text_reply("Done.")               the model stops calling tools (step 2)
    summary_reply([(fact, call_id)])  the structured evidence summary (step 3)

`simple_investigation()` is the shortest full run: one get_order call, one fact.
"""

from collections.abc import Sequence

from support_agent.llm.fake import ScriptStep, text_reply, tool_reply
from support_agent.llm.types import ProviderReply


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
