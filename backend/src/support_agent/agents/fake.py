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


def simple_investigation(order_number: str = "ORD-1001") -> list[ScriptStep]:
    return [
        plan_reply(),
        read_call("get_order", order_number, "call_1"),
        text_reply("Done."),
        summary_reply([("The order exists.", "call_1")]),
    ]
