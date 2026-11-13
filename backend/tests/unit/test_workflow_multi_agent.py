"""The multi-agent workflow end to end with fakes: no database, no network, no real model.

Covers the supervisor's limits, the parallel merge, the extra investigation round and every
outcome path: refund (proposal and pending approval), explain, request info, escalate, and
LLM failure. The database-backed versions are in tests/integration/test_workflow_*.py.
"""

from uuid import uuid4

from support_agent.agents.fake import (
    AgentFake,
    critique_reply,
    draft_reply,
    plan_reply,
    read_call,
    search_call,
    summary_reply,
    supervisor_reply,
)
from support_agent.domain.eligibility import EligibilityDecision, ReasonCode
from support_agent.domain.enums import RunStatus
from support_agent.llm.fake import ScriptStep, text_reply, tool_reply, transient
from support_agent.llm.usage import metered
from support_agent.workflow.fake import (
    CITATION,
    PRIYA_AMOUNT,
    FakeWorld,
    classified,
    fake_deps,
    refund_scripts,
    run_workflow,
    scripted_client,
)
from support_agent.workflow.graph import initial_state
from support_agent.workflow.state import WorkflowError, WorkflowState

CUSTOMER = uuid4()


async def run(
    scripts: dict[str, list[ScriptStep]], world: FakeWorld | None = None
) -> tuple[WorkflowState, AgentFake, FakeWorld]:
    fake = AgentFake(scripts)
    world = world or FakeWorld()
    deps = fake_deps(scripted_client(fake), world, customer_id=uuid4())
    state = initial_state(
        run_id=uuid4(),
        ticket_id=uuid4(),
        customer_id=CUSTOMER,
        complaint_text="My refund has not arrived. ORD-1001",
        ticket_order_number="ORD-1001",
    )
    return await run_workflow(deps, state), fake, world


# ---------------------------------------------------------------- the supervisor's limits


async def test_the_supervisor_cannot_skip_a_required_specialist() -> None:
    scripts = refund_scripts()
    scripts["supervisor"] = [supervisor_reply(investigator=False, policy=False)]

    final, fake, _ = await run(scripts)

    plan = final.get("supervisor")
    assert plan is not None and plan.added_by_code == ["investigator", "policy"]
    # Both specialists really ran, whatever the supervisor chose.
    assert fake.requests_by_agent["investigator"] and fake.requests_by_agent["policy"]
    assert final["status"] == RunStatus.AWAITING_APPROVAL


async def test_order_status_may_skip_the_policy_agent_and_still_resolves() -> None:
    scripts: dict[str, list[ScriptStep]] = {
        "classifier": [classified("order_status")],
        "supervisor": [supervisor_reply(policy=False)],
        "investigator": [
            plan_reply(),
            read_call("get_order", "ORD-1001", "call_1"),
            text_reply("Done."),
            summary_reply([("The order was delivered.", "call_1")]),
        ],
        "resolution": [draft_reply("explain_status", message="Your order was delivered.")],
        "critic": [critique_reply("approve")],
    }

    final, fake, world = await run(scripts)

    assert "policy" not in final and not fake.requests_by_agent["policy"]
    assert final["status"] == RunStatus.COMPLETED
    assert final.get("customer_reply") == "Your order was delivered."
    assert world.proposals == []


async def test_the_supervisors_hints_reach_the_specialists_as_untrusted_data() -> None:
    scripts = refund_scripts()
    scripts["supervisor"] = [
        supervisor_reply(investigator_focus="check the return", policy_focus="look at windows")
    ]

    _, fake, _ = await run(scripts)

    investigator_text = fake.requests_by_agent["investigator"][0].messages[1].content
    policy_text = fake.requests_by_agent["policy"][0].messages[1].content
    assert '<supervisor_hint trust="untrusted_data">' in investigator_text
    assert "check the return" in investigator_text
    assert "look at windows" in policy_text


# ---------------------------------------------------------------- parallel branches and merging


async def test_both_specialists_run_at_the_same_time_each_with_its_own_session() -> None:
    final, _, world = await run(refund_scripts())

    assert world.sessions.max_open == 2  # the branches overlapped
    assert world.sessions.opened == 2
    # Each wrote its own key; neither overwrote the other.
    assert final.get("investigation") is not None and final.get("policy") is not None
    investigation = final["investigation"]
    policy = final["policy"]
    assert investigation is not None and policy is not None
    assert investigation.evidence is not None and policy.findings is not None
    assert [r.tool for r in policy.trace] == ["search_policy"]
    assert "search_policy" not in {r.tool for r in investigation.trace}
    assert final["errors"] == []


async def test_tools_in_both_branches_run_as_the_state_customer() -> None:
    _, _, world = await run(refund_scripts())

    assert {customer for _, customer in world.calls} == {CUSTOMER}
    assert {tool for tool, _ in world.calls} >= {"get_order", "search_policy"}


async def test_errors_from_both_branches_are_merged_not_lost() -> None:
    scripts = refund_scripts()
    scripts["investigator"] = [transient()]  # the investigator's very first call fails
    scripts["policy"] = [transient()]  # and so does the policy agent's

    final, _, world = await run(scripts)

    assert final["errors"] == [WorkflowError.LLM_UNAVAILABLE, WorkflowError.LLM_UNAVAILABLE]
    assert final["status"] == RunStatus.ESCALATED
    assert world.proposals == []


async def test_one_failing_branch_escalates_and_keeps_the_other_branchs_result() -> None:
    scripts = refund_scripts()
    bad_findings = tool_reply("policy_findings_draft", {"rules": "not a list"})
    scripts["policy"] = [
        search_call("window", "ORD-1001", "s1"),
        text_reply("Done."),
        bad_findings,
        bad_findings,  # the answer and the one repair attempt
    ]

    final, _, world = await run(scripts)

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.POLICY_OUTPUT_INVALID]
    investigation = final.get("investigation")
    assert investigation is not None and investigation.evidence is not None
    assert "outcome" not in final and world.proposals == []


# ---------------------------------------------------------------- required evidence and facts


async def test_code_fetches_the_required_records_and_computes_the_facts() -> None:
    # The investigator only called get_order. Code adds get_shipment (the order was delivered).
    final, _, world = await run(refund_scripts())

    investigation = final["investigation"]
    assert investigation is not None
    assert "required_get_shipment" in [r.call_id for r in investigation.trace]
    assert world.called("get_shipment") == 1
    assert final.get("eligibility") == world.decision
    delay = final.get("delay_facts")
    assert delay is not None and delay.delivered


# ---------------------------------------------------------------- the one extra round


def retry_scripts(second_round_ok: bool) -> dict[str, list[ScriptStep]]:
    scripts = refund_scripts()
    first: list[ScriptStep] = [
        plan_reply(),
        read_call("get_order", "ORD-1001", "call_1"),
        text_reply("Done."),
        summary_reply([("The order lookup failed.", "call_1")], missing=["the order record"]),
    ]
    second: list[ScriptStep] = [
        plan_reply(),
        read_call("get_order", "ORD-1001", "call_1"),  # the same id as round one's
        text_reply("Done."),
        summary_reply([("The order was delivered.", "call_1")]),
    ]
    scripts["investigator"] = first + second
    scripts["resolution"] = [
        draft_reply("refund", call_ids=["r2_call_1"], citations=[CITATION])
        if second_round_ok
        else draft_reply("escalate", message="A person will look at this.", call_ids=[])
    ]
    return scripts


async def test_a_missing_required_record_triggers_exactly_one_extra_round() -> None:
    world = FakeWorld()
    # The investigator's get_order fails, then code's own fetch fails, then round two works.
    world.fail_counts["get_order"] = 2

    final, fake, world = await run(retry_scripts(second_round_ok=True), world)

    assert len(fake.requests_by_agent["investigator"]) == 8  # two rounds of four calls
    assert "investigation_retry" in final
    merged = final["investigation"]
    assert merged is not None
    trace_ids = [r.call_id for r in merged.trace]
    assert "call_1" in trace_ids and "r2_call_1" in trace_ids  # ids never clash
    assert final["status"] == RunStatus.AWAITING_APPROVAL
    assert world.proposals == [("ORD-1001", PRIYA_AMOUNT)]


async def test_the_extra_round_happens_at_most_once() -> None:
    world = FakeWorld()
    world.fail_counts["get_order"] = 100  # never works

    final, fake, _ = await run(retry_scripts(second_round_ok=False), world)

    assert len(fake.requests_by_agent["investigator"]) == 8  # not a third round
    assert final["status"] == RunStatus.ESCALATED


# ---------------------------------------------------------------- every outcome path


async def test_refund_creates_a_proposal_for_the_eligible_amount_and_awaits_approval() -> None:
    # A model-supplied amount is ignored: the proposal uses the eligibility decision's.
    scripts = refund_scripts(
        draft=draft_reply("refund", citations=[CITATION], amount_minor=1, amount=1)
    )

    final, _, world = await run(scripts)

    assert final["status"] == RunStatus.AWAITING_APPROVAL
    assert world.proposals == [("ORD-1001", PRIYA_AMOUNT)]
    assert "proposal_id" in final
    reply = final.get("customer_reply", "")
    assert "awaiting approval" in reply


async def test_request_info_saves_the_question_and_waits_for_the_customer() -> None:
    scripts = refund_scripts()
    scripts["resolution"] = [
        draft_reply("request_info", message="Which item arrived damaged?", citations=[])
    ]

    final, _, world = await run(scripts)

    assert final["status"] == RunStatus.AWAITING_CUSTOMER
    assert final.get("customer_reply") == "Which item arrived damaged?"
    assert world.proposals == []


async def test_explain_status_saves_the_reply_and_completes() -> None:
    scripts = refund_scripts()
    scripts["resolution"] = [
        draft_reply("explain_status", message="Your order was delivered on 25 October.")
    ]

    final, _, world = await run(scripts)

    assert final["status"] == RunStatus.COMPLETED
    assert final.get("customer_reply") == "Your order was delivered on 25 October."
    assert world.proposals == []


async def test_a_draft_that_chooses_escalate_escalates() -> None:
    scripts = refund_scripts()
    scripts["resolution"] = [draft_reply("escalate", message="A person will look at this.")]

    final, _, world = await run(scripts)

    assert final["status"] == RunStatus.ESCALATED
    assert world.proposals == []


async def test_a_refund_for_an_ineligible_order_is_rejected_by_the_validator_and_escalates() -> (
    None
):

    blocked = EligibilityDecision(
        eligible=False, refundable_amount_minor=0, reasons=[ReasonCode.NO_RETURN]
    )
    scripts = refund_scripts()
    scripts["resolution"] = [draft_reply("refund", citations=[CITATION])] * 2  # and the revision

    final, _, world = await run(scripts, FakeWorld(decision=blocked))

    assert final["status"] == RunStatus.ESCALATED
    assert world.proposals == []  # nothing is proposed for an order code says is not eligible
    outcome = final.get("outcome")
    assert outcome is not None and "REFUND_NOT_ELIGIBLE" in outcome.reason_codes


async def test_llm_failure_while_resolving_escalates_and_proposes_nothing() -> None:
    scripts = refund_scripts()
    scripts["resolution"] = [transient()]

    final, _, world = await run(scripts)

    assert final["status"] == RunStatus.ESCALATED
    assert WorkflowError.LLM_UNAVAILABLE in final["errors"]
    assert world.proposals == []


async def test_llm_failure_in_the_supervisor_escalates_before_any_specialist_runs() -> None:
    scripts = refund_scripts()
    scripts["supervisor"] = [transient()]

    final, fake, world = await run(scripts)

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.LLM_UNAVAILABLE]
    assert not fake.requests_by_agent["investigator"] and not fake.requests_by_agent["policy"]
    assert world.calls == []


async def test_a_refused_proposal_escalates_with_the_refusal_code() -> None:
    world = FakeWorld()
    world.refuse_proposal_with = "PROPOSAL_ALREADY_PENDING"

    final, _, _ = await run(refund_scripts(), world)

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.PROPOSAL_REFUSED]
    assert final.get("proposal_refusal") == "PROPOSAL_ALREADY_PENDING"
    assert "proposal_id" not in final


async def test_low_confidence_never_reaches_the_supervisor() -> None:
    scripts = {"classifier": [classified("refund", 0.3)]}

    final, fake, _ = await run(scripts)

    assert final["status"] == RunStatus.AWAITING_CUSTOMER
    assert not fake.requests_by_agent["supervisor"]


# ---------------------------------------------------------------- usage


async def test_every_llm_call_is_counted_for_the_run() -> None:
    fake = AgentFake(refund_scripts())
    world = FakeWorld()
    deps = fake_deps(scripted_client(fake), world)
    state = initial_state(
        run_id=uuid4(),
        ticket_id=uuid4(),
        customer_id=CUSTOMER,
        complaint_text="refund please",
        ticket_order_number="ORD-1001",
    )

    with metered() as meter:
        await run_workflow(deps, state)

    assert meter.calls == len(fake.requests)  # one fake request = one successful call
    assert meter.input_tokens > 0 and meter.output_tokens > 0
