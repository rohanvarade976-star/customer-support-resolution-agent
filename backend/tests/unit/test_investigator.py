"""The investigator: plan -> tool loop -> grounded evidence, with the fake LLM and fake tools."""

from support_agent.agents.fake import plan_reply, read_call, simple_investigation, summary_reply
from support_agent.agents.investigator import (
    INVESTIGATOR_TOOLS,
    EvidenceSummary,
    Fact,
    InvestigationFailure,
    InvestigationOutcome,
    ground_facts,
    investigate,
    load_prompt,
)
from support_agent.llm.client import RetrySettings, RoutedLLMClient
from support_agent.llm.fake import FakeProvider, ScriptStep, text_reply, tool_reply, transient
from support_agent.llm.tool_loop import OUTCOME_OK, ToolCallRecord
from support_agent.tools.fake import MISSING_ORDER, FakeTools, fake_tool_context
from support_agent.tools.read_tools import build_read_registry

ORDER = "ORD-1001"
COMPLAINT = "My headphones are broken, please help. SECRET-COMPLAINT-TEXT"


def record(call_id: str, outcome: str = OUTCOME_OK) -> ToolCallRecord:
    return ToolCallRecord(call_id=call_id, tool="get_order", outcome=outcome, latency_ms=1)


async def run_investigation(
    script: list[ScriptStep], tools: FakeTools | None = None, *, max_tool_calls: int = 8
) -> tuple[InvestigationOutcome, FakeProvider, FakeTools]:
    tools = tools or FakeTools()
    fake = FakeProvider(script)
    client = RoutedLLMClient(
        [fake],
        RetrySettings(max_attempts=1, backoff_base_seconds=0, backoff_max_seconds=0),
        default_temperature=0.0,
        default_max_tokens=256,
    )
    outcome = await investigate(
        client,
        tools.registry,
        fake_tool_context(),
        complaint_text=COMPLAINT,
        order_number=ORDER,
        max_tool_calls=max_tool_calls,
    )
    return outcome, fake, tools


# ---------------------------------------------------------------- the allowlist


def test_the_allowlist_is_exactly_the_six_read_tools() -> None:
    assert set(INVESTIGATOR_TOOLS) == {
        "get_order",
        "get_return_status",
        "get_payment_status",
        "get_shipment",
        "get_refundable_balance",
        "check_refund_eligibility",
    }


def test_the_real_registry_gives_the_investigator_no_policy_or_write_tools() -> None:
    full = build_read_registry()
    assert "search_policy" in full.names  # it exists, but not for this agent

    restricted = full.restricted(INVESTIGATOR_TOOLS)

    assert sorted(restricted.names) == sorted(INVESTIGATOR_TOOLS)


async def test_the_model_is_only_offered_the_allowlist_and_cannot_use_other_tools() -> None:
    outcome, fake, tools = await run_investigation(
        [
            plan_reply(),
            read_call("search_policy", ORDER, "call_a"),  # in the registry, not allowed
            read_call("execute_approved_refund", ORDER, "call_b"),
            text_reply("Done."),
            summary_reply(),
        ]
    )

    loop_request = fake.requests[1]
    assert sorted(spec.name for spec in loop_request.tools) == sorted(INVESTIGATOR_TOOLS)
    assert tools.calls == []
    assert [r.outcome for r in outcome.trace] == ["TOOL_NOT_ALLOWED", "TOOL_NOT_ALLOWED"]


# ---------------------------------------------------------------- plan, loop, evidence


async def test_full_run_produces_plan_trace_and_grounded_evidence() -> None:
    outcome, fake, tools = await run_investigation(
        [
            plan_reply("Find the refund status.", ["Check the return.", "Check eligibility."]),
            read_call("get_return_status", ORDER, "call_a"),
            read_call("check_refund_eligibility", ORDER, "call_b"),
            text_reply("Done. I did not need to change the plan."),
            summary_reply(
                [("The return was received.", "call_a"), ("The order is eligible.", "call_b")],
                missing=["Reason for the return"],
            ),
        ]
    )

    assert outcome.failure is None and outcome.plan is not None and outcome.evidence is not None
    assert outcome.plan.steps == ["Check the return.", "Check eligibility."]
    assert [c.tool for c in tools.calls] == ["get_return_status", "check_refund_eligibility"]
    assert [r.call_id for r in outcome.trace] == ["call_a", "call_b"]
    assert [f.call_id for f in outcome.evidence.facts] == ["call_a", "call_b"]
    assert outcome.evidence.dropped_facts == []
    assert outcome.evidence.missing_information == ["Reason for the return"]
    assert fake.requests[0].forced_tool == "investigation_plan"
    assert fake.requests[-1].forced_tool == "evidence_summary"
    assert all(request.tier == "large" for request in fake.requests)


async def test_complaint_text_is_data_never_part_of_the_system_message() -> None:
    _, fake, _ = await run_investigation(simple_investigation(ORDER))

    for request in fake.requests:
        system = [m for m in request.messages if m.role == "system"]
        assert len(system) == 1 and request.messages[0].role == "system"
        assert "SECRET-COMPLAINT-TEXT" not in system[0].content
        user_text = "\n".join(m.content for m in request.messages if m.role == "user")
        assert 'trust="untrusted_data"' in user_text
        assert "SECRET-COMPLAINT-TEXT" in user_text


async def test_tool_results_reach_the_summary_step() -> None:
    tools = FakeTools()
    tools.detail = "RESULT-MARKER"

    _, fake, _ = await run_investigation(simple_investigation(ORDER), tools)

    summary_request = fake.requests[-1]
    tool_text = "\n".join(m.content for m in summary_request.messages if m.role == "tool")
    assert "RESULT-MARKER" in tool_text and 'call_id="call_1"' in tool_text


# ---------------------------------------------------------------- grounding


def test_ground_facts_keeps_only_facts_backed_by_a_successful_call() -> None:
    summary = EvidenceSummary(
        facts=[
            Fact(statement="Real fact.", call_id="call_ok"),
            Fact(statement="Invented call.", call_id="call_never_happened"),
            Fact(statement="Cites a failed call.", call_id="call_failed"),
        ],
        missing_information=["Something"],
    )
    trace = [record("call_ok"), record("call_failed", "ORDER_NOT_FOUND")]

    evidence = ground_facts(summary, trace)

    assert [f.statement for f in evidence.facts] == ["Real fact."]
    assert [f.statement for f in evidence.dropped_facts] == [
        "Invented call.",
        "Cites a failed call.",
    ]
    assert evidence.missing_information == ["Something"]


async def test_ungrounded_fact_in_a_full_run_is_dropped_and_flagged() -> None:
    outcome, _, _ = await run_investigation(
        [
            plan_reply(),
            read_call("get_order", ORDER, "call_a"),
            text_reply("Done."),
            summary_reply([("Real fact.", "call_a"), ("Made up.", "call_zzz")]),
        ]
    )

    assert outcome.evidence is not None
    assert [f.statement for f in outcome.evidence.facts] == ["Real fact."]
    assert [f.statement for f in outcome.evidence.dropped_facts] == ["Made up."]


async def test_fact_citing_a_failed_lookup_is_dropped_and_no_foreign_data_survives() -> None:
    outcome, _, _ = await run_investigation(
        [
            plan_reply(),
            read_call("get_order", MISSING_ORDER, "call_a"),  # not this customer's order
            text_reply("Done."),
            summary_reply([("The order holds headphones.", "call_a")]),
        ]
    )

    assert outcome.evidence is not None
    assert outcome.evidence.facts == []
    assert len(outcome.evidence.dropped_facts) == 1
    assert [r.outcome for r in outcome.trace] == ["ORDER_NOT_FOUND"]


# ---------------------------------------------------------------- failures keep the trace


async def test_step_limit_fails_with_the_trace_and_no_evidence() -> None:
    outcome, _, tools = await run_investigation(
        [plan_reply(), *(read_call("get_order", ORDER, f"call_{n}") for n in range(4))],
        max_tool_calls=3,
    )

    assert outcome.failure == InvestigationFailure.STEP_LIMIT
    assert outcome.evidence is None and outcome.plan is not None
    assert len(outcome.trace) == 3 and len(tools.calls) == 3


async def test_llm_down_at_the_planning_step() -> None:
    outcome, _, _ = await run_investigation([transient()])

    assert outcome.failure == InvestigationFailure.LLM_UNAVAILABLE
    assert outcome.plan is None and outcome.trace == [] and outcome.evidence is None


async def test_llm_down_midway_keeps_the_calls_already_made() -> None:
    outcome, _, _ = await run_investigation(
        [plan_reply(), read_call("get_order", ORDER, "call_a"), transient()]
    )

    assert outcome.failure == InvestigationFailure.LLM_UNAVAILABLE
    assert [r.call_id for r in outcome.trace] == ["call_a"]
    assert outcome.plan is not None


async def test_invalid_summary_after_one_repair_fails_with_output_invalid() -> None:
    bad = tool_reply("evidence_summary", {"facts": "not a list"})
    outcome, _, _ = await run_investigation(
        [plan_reply(), read_call("get_order", ORDER, "call_a"), text_reply("Done."), bad, bad]
    )

    assert outcome.failure == InvestigationFailure.OUTPUT_INVALID
    assert [r.call_id for r in outcome.trace] == ["call_a"]


async def test_invalid_plan_after_one_repair_fails_with_output_invalid() -> None:
    bad = tool_reply("investigation_plan", {"goal": "x", "steps": []})
    outcome, _, _ = await run_investigation([bad, bad])

    assert outcome.failure == InvestigationFailure.OUTPUT_INVALID
    assert outcome.plan is None


def test_prompt_tells_the_model_that_data_is_not_instructions() -> None:
    prompt = load_prompt()

    assert "untrusted_data" in prompt and "check_refund_eligibility" in prompt


async def test_the_outcome_exposes_the_structured_outputs_of_successful_calls_only() -> None:
    outcome, _, _ = await run_investigation(
        [
            plan_reply(),
            read_call("get_order", ORDER, "call_ok"),
            read_call("get_order", MISSING_ORDER, "call_bad"),
            text_reply("Done."),
            summary_reply([("The order exists.", "call_ok")]),
        ]
    )

    assert [(r.call_id, r.tool) for r in outcome.results] == [("call_ok", "get_order")]
    assert outcome.results[0].args == {"order_number": ORDER}
    assert [r.call_id for r in outcome.trace] == ["call_ok", "call_bad"]


async def test_a_failed_investigation_still_exposes_the_results_gathered_so_far() -> None:
    outcome, _, _ = await run_investigation(
        [plan_reply(), read_call("get_order", ORDER, "call_1"), transient()]
    )

    assert outcome.failure is not None
    assert [r.call_id for r in outcome.results] == ["call_1"]
