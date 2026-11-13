"""The LangGraph workflow: every route, with the fake LLM, fake tools and a fake ownership check.

No database here: ownership is a plain function that knows which orders the test
customer owns, and the tools are canned fakes. The database versions are covered by
tests/integration/test_runs_api.py and test_investigation.py.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest

from support_agent.agents.fake import plan_reply, read_call, simple_investigation, summary_reply
from support_agent.domain.enums import RunStatus
from support_agent.llm.client import RetrySettings, RoutedLLMClient
from support_agent.llm.errors import LLMError
from support_agent.llm.fake import FakeProvider, ScriptStep, text_reply, tool_reply, transient
from support_agent.tools.fake import FakeTools, fake_tool_context
from support_agent.workflow.graph import WorkflowDeps, initial_state, run_workflow
from support_agent.workflow.nodes.classify import normalize_order_number
from support_agent.workflow.nodes.intake import intake
from support_agent.workflow.nodes.placeholders import escalate, needs_clarification
from support_agent.workflow.state import OrderHint, WorkflowError, WorkflowState

TOOL = "classification"
CUSTOMER = uuid4()
OWNED_ORDERS = {"ORD-1001"}  # what the test customer owns
THRESHOLD = 0.7
MAX_TOOL_CALLS = 3


def answer(category: str, confidence: float, order_number: str | None = None) -> ScriptStep:
    args: dict[str, Any] = {"category": category, "confidence": confidence, "reason": "test"}
    if order_number is not None:
        args["order_number"] = order_number
    return tool_reply(TOOL, args)


async def owns_order(customer_id: UUID, order_number: str) -> bool:
    return customer_id == CUSTOMER and order_number in OWNED_ORDERS


def make_state(ticket_order_number: str | None = None) -> WorkflowState:
    return initial_state(
        run_id=uuid4(),
        ticket_id=uuid4(),
        customer_id=CUSTOMER,
        complaint_text="Something went wrong with my order.",
        ticket_order_number=ticket_order_number,
    )


async def run(
    script: list[ScriptStep],
    ticket_order_number: str | None = None,
    tools: FakeTools | None = None,
) -> WorkflowState:
    client = RoutedLLMClient(
        [FakeProvider(script)],
        RetrySettings(max_attempts=1, backoff_base_seconds=0, backoff_max_seconds=0),
        default_temperature=0.0,
        default_max_tokens=256,
    )
    deps = WorkflowDeps(
        llm=client,
        owns_order=owns_order,
        confidence_threshold=THRESHOLD,
        registry=(tools or FakeTools()).registry,
        # A context for some other customer: the node must use the state's customer instead.
        tool_context=fake_tool_context(),
        max_tool_calls=MAX_TOOL_CALLS,
    )
    return await run_workflow(deps, make_state(ticket_order_number))


# ---------------------------------------------------------------- routing


async def test_confident_classification_goes_to_investigate() -> None:
    final = await run([answer("refund", 0.95), *simple_investigation()], "ORD-1001")

    assert final["status"] == RunStatus.INVESTIGATING
    classification = final.get("classification")
    assert classification is not None and classification.category == "refund"
    assert final["errors"] == []
    plan = final.get("investigation_plan")
    evidence = final.get("evidence")
    assert plan is not None and evidence is not None
    assert [f.call_id for f in evidence.facts] == ["call_1"]
    assert [r.tool for r in final.get("tool_trace", [])] == ["get_order"]


async def test_confidence_exactly_at_the_threshold_investigates() -> None:
    final = await run([answer("order_status", THRESHOLD), *simple_investigation()], "ORD-1001")

    assert final["status"] == RunStatus.INVESTIGATING


async def test_low_confidence_asks_for_clarification() -> None:
    final = await run([answer("refund", 0.4)])

    assert final["status"] == RunStatus.AWAITING_CUSTOMER


async def test_unclear_category_asks_for_clarification_even_if_confident() -> None:
    final = await run([answer("unclear", 0.99)])

    assert final["status"] == RunStatus.AWAITING_CUSTOMER


async def test_llm_unavailable_escalates() -> None:
    # One provider, one attempt, a transient failure -> LLMUnavailableError.
    final = await run([transient()])

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.LLM_UNAVAILABLE]
    assert final.get("classification") is None


async def test_invalid_classifier_output_escalates() -> None:
    bad = tool_reply(TOOL, {"category": "billing", "confidence": 0.9, "reason": "r"})
    final = await run([bad, bad])  # first answer + the one repair attempt

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.CLASSIFIER_OUTPUT_INVALID]


async def test_other_llm_errors_are_not_swallowed() -> None:
    # A bad key or bad request is a setup bug: the run must fail loudly, not escalate.
    with pytest.raises(LLMError):
        await run([LLMError("bad request")])


# ---------------------------------------------------------------- order number hints


async def test_owned_order_hint_is_used_when_the_ticket_names_no_order() -> None:
    final = await run([answer("refund", 0.9, order_number="ord 1001"), *simple_investigation()])

    assert final.get("order_hint") == OrderHint.VERIFIED
    assert final.get("order_number") == "ORD-1001"


async def test_foreign_order_hint_is_ignored_and_recorded() -> None:
    # ORD-2002 belongs to someone else (or does not exist): the same thing to us.
    final = await run([answer("refund", 0.9, order_number="ORD-2002")])

    assert final.get("order_hint") == OrderHint.IGNORED_NOT_OWNED
    assert final.get("order_number") is None
    classification = final.get("classification")
    assert classification is not None and classification.category == "refund"  # label still used
    # No verified order, so the investigator is not run (the script has no answers for it).
    assert final["status"] == RunStatus.AWAITING_CUSTOMER


async def test_hint_that_differs_from_the_ticket_order_is_ignored() -> None:
    final = await run(
        [answer("refund", 0.9, order_number="ORD-2002"), *simple_investigation()],
        ticket_order_number="ORD-1001",
    )

    assert final.get("order_hint") == OrderHint.IGNORED_CONFLICTS_TICKET
    assert final.get("order_number") == "ORD-1001"


async def test_hint_matching_the_ticket_order() -> None:
    final = await run(
        [answer("refund", 0.9, order_number="#1001"), *simple_investigation()],
        ticket_order_number="ORD-1001",
    )

    assert final.get("order_hint") == OrderHint.MATCHED_TICKET
    assert final.get("order_number") == "ORD-1001"


async def test_no_hint_keeps_the_ticket_order() -> None:
    final = await run(
        [answer("refund", 0.9), *simple_investigation()], ticket_order_number="ORD-1001"
    )

    assert final.get("order_hint") == OrderHint.NONE
    assert final.get("order_number") == "ORD-1001"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ORD-1042", "ORD-1042"),
        ("ord-1042", "ORD-1042"),
        ("order 1042", "ORD-1042"),
        ("#1042", "ORD-1042"),
        ("1042", "ORD-1042"),
        (None, None),
        ("", None),
        ("my last order", None),
        ("ORD-1042; DROP TABLE orders", None),
    ],
)
def test_normalize_order_number(raw: str | None, expected: str | None) -> None:
    assert normalize_order_number(raw) == expected


# ---------------------------------------------------------------- nodes return only changes


async def test_nodes_return_only_the_fields_they_change() -> None:
    state = make_state("ORD-1001")

    assert await intake(state) == {"status": RunStatus.INVESTIGATING, "order_number": "ORD-1001"}
    assert await needs_clarification(state) == {"status": RunStatus.AWAITING_CUSTOMER}
    assert await escalate(state) == {"status": RunStatus.ESCALATED}


# ---------------------------------------------------------------- the investigator in the graph


async def test_confident_run_without_a_verified_order_asks_for_it_and_makes_no_llm_call() -> None:
    # The script holds only the classifier's answer: any investigator call would raise.
    tools = FakeTools()
    final = await run([answer("refund", 0.95)], tools=tools)

    assert final["status"] == RunStatus.AWAITING_CUSTOMER
    assert final.get("missing_information") == ["order_number"]
    assert "tool_trace" not in final and tools.calls == []


async def test_tools_run_as_the_state_customer_not_the_context_customer() -> None:
    tools = FakeTools()
    await run([answer("refund", 0.95), *simple_investigation()], "ORD-1001", tools)

    assert [call.customer_id for call in tools.calls] == [CUSTOMER]


async def test_step_limit_escalates_and_keeps_the_trace() -> None:
    tools = FakeTools()
    too_many = [read_call("get_order", "ORD-1001", f"call_{n}") for n in range(MAX_TOOL_CALLS + 1)]

    final = await run([answer("refund", 0.95), plan_reply(), *too_many], "ORD-1001", tools)

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.INVESTIGATION_STEP_LIMIT]
    assert len(final.get("tool_trace", [])) == MAX_TOOL_CALLS == len(tools.calls)
    assert final.get("evidence") is None and final.get("investigation_plan") is not None


async def test_llm_failure_during_investigation_escalates() -> None:
    script = [
        answer("refund", 0.95),
        plan_reply(),
        read_call("get_order", "ORD-1001", "call_1"),
        transient(),
    ]

    final = await run(script, "ORD-1001")

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.LLM_UNAVAILABLE]
    assert [r.call_id for r in final.get("tool_trace", [])] == ["call_1"]


async def test_invalid_investigator_output_escalates() -> None:
    bad = tool_reply("evidence_summary", {"facts": "not a list"})
    script = [
        answer("refund", 0.95),
        plan_reply(),
        read_call("get_order", "ORD-1001", "call_1"),
        text_reply("Done."),
        bad,
        bad,
    ]

    final = await run(script, "ORD-1001")

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.INVESTIGATOR_OUTPUT_INVALID]


async def test_a_summary_with_only_ungrounded_facts_still_completes_with_them_flagged() -> None:
    script = [
        answer("refund", 0.95),
        plan_reply(),
        read_call("get_order", "ORD-1001", "call_1"),
        text_reply("Done."),
        summary_reply([("Invented.", "call_nope")]),
    ]

    final = await run(script, "ORD-1001")

    evidence = final.get("evidence")
    assert final["status"] == RunStatus.INVESTIGATING and final["errors"] == []
    assert evidence is not None and evidence.facts == [] and len(evidence.dropped_facts) == 1
