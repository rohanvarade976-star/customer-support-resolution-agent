"""The LangGraph workflow's routing, with the fake LLM, fake tools and a fake ownership check.

No database here: ownership is a plain function that knows which orders the test
customer owns, and the tools and the case facts are canned fakes (workflow/fake.py).
The multi-agent paths are covered in test_workflow_multi_agent.py and the baseline in
test_baseline.py; the database versions are tests/integration/test_runs_api.py and
test_investigation.py.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest

from support_agent.agents.fake import AgentFake, plan_reply, read_call, summary_reply
from support_agent.domain.enums import RunStatus
from support_agent.llm.errors import LLMError
from support_agent.llm.fake import ScriptStep, text_reply, tool_reply, transient
from support_agent.workflow.fake import FakeWorld, fake_deps, refund_scripts, scripted_client
from support_agent.workflow.graph import initial_state, run_workflow
from support_agent.workflow.nodes.classify import normalize_order_number
from support_agent.workflow.nodes.finish import escalate
from support_agent.workflow.nodes.intake import intake
from support_agent.workflow.nodes.placeholders import ask_for_order, needs_clarification
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


def full_run(classifier: ScriptStep) -> dict[str, list[ScriptStep]]:
    """Scripts for a complete run, with the given classifier answer."""
    return {**refund_scripts(), "classifier": [classifier]}


async def run(
    scripts: dict[str, list[ScriptStep]],
    ticket_order_number: str | None = None,
    world: FakeWorld | None = None,
) -> WorkflowState:
    deps = fake_deps(
        scripted_client(AgentFake(scripts)),
        world or FakeWorld(),
        confidence_threshold=THRESHOLD,
        max_tool_calls=MAX_TOOL_CALLS,
        owns_order=owns_order,
    )
    return await run_workflow(deps, make_state(ticket_order_number))


# ---------------------------------------------------------------- routing


async def test_confident_classification_goes_to_the_supervisor_and_on() -> None:
    final = await run(full_run(answer("refund", 0.95)), "ORD-1001")

    classification = final.get("classification")
    assert classification is not None and classification.category == "refund"
    assert final.get("supervisor") is not None and final["errors"] == []
    investigation = final.get("investigation")
    assert investigation is not None and investigation.plan is not None
    assert [r.tool for r in investigation.trace][:1] == ["get_order"]


async def test_confidence_exactly_at_the_threshold_continues() -> None:
    final = await run(full_run(answer("refund", THRESHOLD)), "ORD-1001")

    assert final.get("supervisor") is not None


async def test_low_confidence_asks_for_clarification() -> None:
    final = await run({"classifier": [answer("refund", 0.4)]})

    assert final["status"] == RunStatus.AWAITING_CUSTOMER


async def test_unclear_category_asks_for_clarification_even_if_confident() -> None:
    final = await run({"classifier": [answer("unclear", 0.99)]})

    assert final["status"] == RunStatus.AWAITING_CUSTOMER


async def test_llm_unavailable_escalates() -> None:
    # One provider, one attempt, a transient failure -> LLMUnavailableError.
    final = await run({"classifier": [transient()]})

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.LLM_UNAVAILABLE]
    assert final.get("classification") is None


async def test_invalid_classifier_output_escalates() -> None:
    bad = tool_reply(TOOL, {"category": "billing", "confidence": 0.9, "reason": "r"})
    final = await run({"classifier": [bad, bad]})  # first answer + the one repair attempt

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.CLASSIFIER_OUTPUT_INVALID]


async def test_other_llm_errors_are_not_swallowed() -> None:
    # A bad key or bad request is a setup bug: the run must fail loudly, not escalate.
    with pytest.raises(LLMError):
        await run({"classifier": [LLMError("bad request")]})


# ---------------------------------------------------------------- order number hints


async def test_owned_order_hint_is_used_when_the_ticket_names_no_order() -> None:
    final = await run(full_run(answer("refund", 0.9, order_number="ord 1001")))

    assert final.get("order_hint") == OrderHint.VERIFIED
    assert final.get("order_number") == "ORD-1001"
    assert final.get("supervisor") is not None


async def test_foreign_order_hint_is_ignored_and_recorded() -> None:
    # ORD-2002 belongs to someone else (or does not exist): the same thing to us.
    final = await run({"classifier": [answer("refund", 0.9, order_number="ORD-2002")]})

    assert final.get("order_hint") == OrderHint.IGNORED_NOT_OWNED
    assert final.get("order_number") is None
    classification = final.get("classification")
    assert classification is not None and classification.category == "refund"  # label still used
    # No verified order: the customer is asked for it and no specialist runs.
    assert final["status"] == RunStatus.AWAITING_CUSTOMER
    assert final.get("missing_information") == ["order_number"]
    assert final.get("supervisor") is None


async def test_hint_that_differs_from_the_ticket_order_is_ignored() -> None:
    final = await run(
        full_run(answer("refund", 0.9, order_number="ORD-2002")), ticket_order_number="ORD-1001"
    )

    assert final.get("order_hint") == OrderHint.IGNORED_CONFLICTS_TICKET
    assert final.get("order_number") == "ORD-1001"


async def test_hint_matching_the_ticket_order() -> None:
    final = await run(
        full_run(answer("refund", 0.9, order_number="#1001")), ticket_order_number="ORD-1001"
    )

    assert final.get("order_hint") == OrderHint.MATCHED_TICKET
    assert final.get("order_number") == "ORD-1001"


async def test_no_hint_keeps_the_ticket_order() -> None:
    final = await run(full_run(answer("refund", 0.9)), ticket_order_number="ORD-1001")

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
    assert await ask_for_order(state) == {
        "status": RunStatus.AWAITING_CUSTOMER,
        "missing_information": ["order_number"],
    }
    escalated = await escalate(state)
    assert set(escalated) == {"status", "customer_reply"}
    assert escalated["status"] == RunStatus.ESCALATED


# ---------------------------------------------------------------- the investigator in the graph


async def test_confident_run_without_a_verified_order_asks_for_it_and_calls_no_specialist() -> None:
    # The script holds only the classifier's answer: any other agent call would raise.
    world = FakeWorld()
    final = await run({"classifier": [answer("refund", 0.95)]}, world=world)

    assert final["status"] == RunStatus.AWAITING_CUSTOMER
    assert final.get("missing_information") == ["order_number"]
    assert final.get("investigation") is None and world.calls == []


async def test_tools_run_as_the_state_customer_not_the_context_customer() -> None:
    world = FakeWorld()
    await run(full_run(answer("refund", 0.95)), "ORD-1001", world)

    assert {customer for _, customer in world.calls} == {CUSTOMER}


async def test_step_limit_escalates_and_keeps_the_trace() -> None:
    too_many = [read_call("get_order", "ORD-1001", f"call_{n}") for n in range(MAX_TOOL_CALLS + 1)]
    scripts = full_run(answer("refund", 0.95))
    scripts["investigator"] = [plan_reply(), *too_many]

    final = await run(scripts, "ORD-1001")

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.INVESTIGATION_STEP_LIMIT]
    investigation = final.get("investigation")
    assert investigation is not None and investigation.evidence is None
    assert len(investigation.trace) == MAX_TOOL_CALLS
    assert investigation.plan is not None


async def test_llm_failure_during_investigation_escalates() -> None:
    scripts = full_run(answer("refund", 0.95))
    scripts["investigator"] = [
        plan_reply(),
        read_call("get_order", "ORD-1001", "call_1"),
        transient(),
    ]

    final = await run(scripts, "ORD-1001")

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.LLM_UNAVAILABLE]
    investigation = final.get("investigation")
    assert investigation is not None
    assert [r.call_id for r in investigation.trace] == ["call_1"]


async def test_invalid_investigator_output_escalates() -> None:
    bad = tool_reply("evidence_summary", {"facts": "not a list"})
    scripts = full_run(answer("refund", 0.95))
    scripts["investigator"] = [
        plan_reply(),
        read_call("get_order", "ORD-1001", "call_1"),
        text_reply("Done."),
        bad,
        bad,
    ]

    final = await run(scripts, "ORD-1001")

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.INVESTIGATOR_OUTPUT_INVALID]


async def test_a_summary_with_only_ungrounded_facts_still_continues_with_them_flagged() -> None:
    scripts = full_run(answer("refund", 0.95))
    scripts["investigator"] = [
        plan_reply(),
        read_call("get_order", "ORD-1001", "call_1"),
        text_reply("Done."),
        summary_reply([("Invented.", "call_nope")]),
    ]

    final = await run(scripts, "ORD-1001")

    investigation = final.get("investigation")
    assert final["errors"] == []
    assert investigation is not None and investigation.evidence is not None
    # The invented fact is dropped. Only facts code wrote (the shipment record) remain.
    assert [f.call_id for f in investigation.evidence.facts] == ["required_get_shipment"]
    assert len(investigation.evidence.dropped_facts) == 1
