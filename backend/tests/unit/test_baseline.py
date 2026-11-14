"""The single-agent baseline: one loop with all read tools, then the same checks as multi-agent.

Fakes only. The point of these tests is that the baseline goes through the same required
evidence, computed facts and validator, so it can never do something the multi-agent
system's code would refuse.
"""

from uuid import uuid4

from support_agent.agents.baseline import BASELINE_TOOLS
from support_agent.agents.fake import AgentFake, draft_reply, read_call, search_call
from support_agent.domain.eligibility import EligibilityDecision, ReasonCode
from support_agent.domain.enums import RunMode, RunStatus
from support_agent.llm.fake import ScriptStep, text_reply, transient
from support_agent.workflow.fake import (
    CITATION,
    PRIYA_AMOUNT,
    FakeWorld,
    classified,
    fake_deps,
    run_workflow,
    scripted_client,
)
from support_agent.workflow.graph import initial_state
from support_agent.workflow.state import WorkflowError, WorkflowState

CUSTOMER = uuid4()


def baseline_scripts(draft: ScriptStep | None = None) -> dict[str, list[ScriptStep]]:
    return {
        "classifier": [classified("refund")],
        # One loop: the order, then the policy, then it stops.
        "baseline": [
            read_call("get_order", "ORD-1001", "call_1"),
            search_call("return window", "ORD-1001", "call_2"),
            text_reply("Done."),
        ],
        "resolution": [draft or draft_reply("refund", call_ids=["call_1"], citations=[CITATION])],
    }


async def run(
    scripts: dict[str, list[ScriptStep]], world: FakeWorld | None = None
) -> tuple[WorkflowState, AgentFake, FakeWorld]:
    fake = AgentFake(scripts)
    world = world or FakeWorld()
    deps = fake_deps(scripted_client(fake), world, mode=RunMode.BASELINE)
    state = initial_state(
        run_id=uuid4(),
        ticket_id=uuid4(),
        customer_id=CUSTOMER,
        complaint_text="My refund has not arrived. ORD-1001",
        ticket_order_number="ORD-1001",
    )
    return await run_workflow(deps, state), fake, world


async def test_baseline_refund_goes_through_to_a_pending_proposal() -> None:
    final, _, world = await run(baseline_scripts())

    assert final["status"] == RunStatus.AWAITING_APPROVAL
    assert world.proposals == [("ORD-1001", PRIYA_AMOUNT)]
    assert world.sessions.opened == 0  # no parallel branches in baseline mode


async def test_baseline_has_no_supervisor_no_specialists_and_no_critic() -> None:
    _, fake, _ = await run(baseline_scripts())

    assert set(fake.requests_by_agent) == {"classifier", "baseline", "resolution"}


async def test_baseline_offers_every_read_tool_and_search_policy_but_no_write_tool() -> None:
    _, fake, _ = await run(baseline_scripts())

    offered = {tool.name for tool in fake.requests_by_agent["baseline"][0].tools}
    assert offered == set(BASELINE_TOOLS)
    assert "execute_approved_refund" not in offered


async def test_baseline_gets_the_same_required_evidence_and_computed_facts() -> None:
    # The model never asked for get_shipment: code fetches it, as in multi-agent mode.
    final, _, world = await run(baseline_scripts())

    assert world.called("get_shipment") == 1
    investigation = final["investigation"]
    assert investigation is not None
    assert "required_get_shipment" in [r.call_id for r in investigation.trace]
    assert final.get("eligibility") == world.decision
    delay = final.get("delay_facts")
    assert delay is not None and delay.delivered


async def test_baseline_draft_is_checked_by_the_same_validator() -> None:
    # An ungrounded policy citation: the validator rejects it, so the run escalates.
    bad = draft_reply("refund", call_ids=["call_1"], citations=["refund_policy_v9:section_9"])

    final, _, world = await run(baseline_scripts(bad))

    assert final["status"] == RunStatus.ESCALATED
    assert world.proposals == []
    outcome = final.get("outcome")
    assert outcome is not None and "CITATION_NOT_GROUNDED" in outcome.reason_codes


async def test_baseline_cannot_refund_an_ineligible_order() -> None:
    blocked = EligibilityDecision(
        eligible=False, refundable_amount_minor=0, reasons=[ReasonCode.NO_RETURN]
    )

    final, _, world = await run(baseline_scripts(), FakeWorld(decision=blocked))

    assert final["status"] == RunStatus.ESCALATED
    assert world.proposals == []
    outcome = final.get("outcome")
    assert outcome is not None and "REFUND_NOT_ELIGIBLE" in outcome.reason_codes


async def test_baseline_amount_comes_from_the_eligibility_decision_only() -> None:
    wrong = draft_reply("refund", call_ids=["call_1"], citations=[CITATION], amount_minor=1)

    _, _, world = await run(baseline_scripts(wrong))

    assert world.proposals == [("ORD-1001", PRIYA_AMOUNT)]


async def test_baseline_llm_failure_while_gathering_escalates() -> None:
    scripts = baseline_scripts()
    scripts["baseline"] = [read_call("get_order", "ORD-1001", "call_1"), transient()]

    final, _, world = await run(scripts)

    assert final["status"] == RunStatus.ESCALATED
    assert final["errors"] == [WorkflowError.LLM_UNAVAILABLE]
    assert world.proposals == []


async def test_baseline_llm_failure_while_drafting_escalates() -> None:
    scripts = baseline_scripts()
    scripts["resolution"] = [transient()]

    final, _, world = await run(scripts)

    assert final["status"] == RunStatus.ESCALATED
    assert WorkflowError.LLM_UNAVAILABLE in final["errors"]
    assert world.proposals == []


async def test_baseline_draft_call_has_no_tool_call_history() -> None:
    # Regression (ADR-016): replaying the gathering tool calls into a call that binds only
    # `resolution_draft` made Gemini answer in plain text instead of the forced call.
    _, fake, _ = await run(baseline_scripts())

    request = fake.requests_by_agent["resolution"][0]
    assert [t.name for t in request.tools] == ["resolution_draft"]
    assert all(m.role in ("system", "user") for m in request.messages)
    assert not any(m.tool_calls or m.tool_call_id for m in request.messages)
    text = "\n".join(m.content for m in request.messages)
    assert 'call_id="call_1"' in text  # the results stay visible and citable
    assert 'trust="untrusted_data"' in text
