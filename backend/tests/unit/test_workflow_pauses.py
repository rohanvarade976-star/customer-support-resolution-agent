"""Pausing and resuming the graph (M7) with fakes and an in-memory checkpointer.

No database, no network, no real model. What is checked:
- the run pauses after a refund proposal, and nothing before the pause runs twice;
- after the reviewer's decision only the STORED decision counts, never the resume value;
- approve executes once, reject reopens, expired escalates, refused revalidation escalates;
- a finished run cannot be resumed again;
- a customer reply resumes from the right step, and the clarification limit escalates;
- a fresh graph (new deps, new LLM client) resumes from the saved checkpoint;
- the checkpoint serializer revives only allowlisted types.

The same flows against Postgres and the real refund executor are in
tests/integration/test_pause_resume.py.
"""

from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from support_agent.agents.fake import (
    AgentFake,
    critique_reply,
    draft_reply,
    policy_run,
    simple_investigation,
    supervisor_reply,
)
from support_agent.agents.investigator import InvestigationOutcome
from support_agent.domain.enums import ApprovalDecision, RunStatus, TicketStatus
from support_agent.llm.fake import ScriptStep, tool_reply
from support_agent.workflow.checkpointer import allowed_types, checkpoint_serializer
from support_agent.workflow.deps import WorkflowDeps
from support_agent.workflow.fake import (
    CITATION,
    FakeWorld,
    classified,
    fake_deps,
    memory_checkpointer,
    refund_scripts,
    scripted_client,
    thread_for,
)
from support_agent.workflow.graph import (
    Checkpointer,
    NotPausedError,
    Segment,
    initial_state,
    resume_workflow,
    start_workflow,
)
from support_agent.workflow.state import AskedFor, WorkflowError, WorkflowState


def new_state(order_number: str | None = "ORD-1001") -> WorkflowState:
    return initial_state(
        run_id=uuid4(),
        ticket_id=uuid4(),
        customer_id=uuid4(),
        complaint_text="My refund has not arrived.",
        ticket_order_number=order_number,
    )


def deps_for(
    scripts: dict[str, list[ScriptStep]], world: FakeWorld, *, max_rounds: int = 2
) -> tuple[WorkflowDeps, AgentFake]:
    fake = AgentFake(scripts)
    return fake_deps(scripted_client(fake), world, max_clarification_rounds=max_rounds), fake


async def start(
    scripts: dict[str, list[ScriptStep]], world: FakeWorld, state: WorkflowState, **kw: int
) -> tuple[Segment, WorkflowDeps, AgentFake, Checkpointer]:
    deps, fake = deps_for(scripts, world, **kw)
    saver = memory_checkpointer(deps)
    return await start_workflow(deps, saver, thread_for(state), state), deps, fake, saver


def classifier_texts(fake: AgentFake) -> list[str]:
    return [
        "\n".join(m.content for m in request.messages)
        for request in fake.requests_by_agent["classifier"]
    ]


# ---------------------------------------------------------------- approval pause


async def test_a_refund_proposal_pauses_the_run_for_approval() -> None:
    world = FakeWorld()
    segment, *_ = await start(refund_scripts(), world, new_state())

    assert segment.paused
    assert segment.state["status"] == RunStatus.AWAITING_APPROVAL
    assert len(world.proposals) == 1
    assert world.executed == []  # no money before a decision


async def test_approve_executes_once_and_nothing_before_the_pause_runs_again() -> None:
    world = FakeWorld()
    state = new_state()
    first, deps, fake, saver = await start(refund_scripts(), world, state)
    calls_before = len(fake.requests)

    world.approval_decision = ApprovalDecision.APPROVED
    done = await resume_workflow(deps, saver, thread_for(state), {"event": "decided"})

    assert not done.paused
    assert done.state["status"] == RunStatus.COMPLETED
    assert done.written.get("ticket_status") == TicketStatus.IN_PROGRESS  # accepted, not paid
    assert world.executed == [first.state.get("proposal_id")]
    assert len(world.proposals) == 1  # propose_refund was not replayed
    assert len(fake.requests) == calls_before  # no model call after the decision


async def test_the_stored_decision_wins_over_the_resume_value() -> None:
    world = FakeWorld()
    state = new_state()
    _, deps, _, saver = await start(refund_scripts(), world, state)

    world.approval_decision = ApprovalDecision.REJECTED
    done = await resume_workflow(deps, saver, thread_for(state), {"decision": "approve"})

    assert world.executed == []
    assert done.state["status"] == RunStatus.COMPLETED
    assert done.written.get("ticket_status") == TicketStatus.OPEN


@pytest.mark.parametrize("decision", [ApprovalDecision.EXPIRED, ApprovalDecision.PENDING, None])
async def test_anything_but_approve_or_reject_escalates(
    decision: ApprovalDecision | None,
) -> None:
    world = FakeWorld()
    state = new_state()
    _, deps, _, saver = await start(refund_scripts(), world, state)

    world.approval_decision = decision
    done = await resume_workflow(deps, saver, thread_for(state), {"event": "decided"})

    assert world.executed == []
    assert done.state["status"] == RunStatus.ESCALATED


async def test_refused_revalidation_escalates_with_a_code() -> None:
    world = FakeWorld()
    state = new_state()
    _, deps, _, saver = await start(refund_scripts(), world, state)

    world.approval_decision = ApprovalDecision.APPROVED
    world.refuse_refund_with = "REVALIDATION_FAILED"
    done = await resume_workflow(deps, saver, thread_for(state), {"event": "decided"})

    assert done.state["status"] == RunStatus.ESCALATED
    assert done.written.get("refund_refusal") == "REVALIDATION_FAILED"
    assert done.written.get("errors") == [WorkflowError.REFUND_NOT_EXECUTED]


async def test_a_finished_run_cannot_be_resumed_again() -> None:
    world = FakeWorld()
    state = new_state()
    _, deps, _, saver = await start(refund_scripts(), world, state)
    world.approval_decision = ApprovalDecision.APPROVED
    await resume_workflow(deps, saver, thread_for(state), {"event": "decided"})

    with pytest.raises(NotPausedError):
        await resume_workflow(deps, saver, thread_for(state), {"event": "decided"})
    assert len(world.executed) == 1


async def test_a_fresh_graph_resumes_from_the_saved_checkpoint() -> None:
    world = FakeWorld()
    state = new_state()
    _, _, _, saver = await start(refund_scripts(), world, state)

    # A "restart": new deps and a new LLM client with no script left. Only the saver is shared.
    fresh_deps, _ = deps_for({}, world)
    world.approval_decision = ApprovalDecision.APPROVED
    done = await resume_workflow(fresh_deps, saver, thread_for(state), {"event": "decided"})

    assert done.state["status"] == RunStatus.COMPLETED
    assert len(world.executed) == 1


# ---------------------------------------------------------------- customer pause


async def test_a_missing_order_pauses_then_the_reply_is_classified_with_the_complaint() -> None:
    scripts = refund_scripts()
    scripts["classifier"] = [
        classified("refund"),  # no order number anywhere: ask for one
        tool_reply(  # the reply names one; code still checks this customer owns it
            "classification",
            {"category": "refund", "confidence": 0.95, "reason": "t", "order_number": "1001"},
        ),
    ]
    world = FakeWorld()
    state = new_state(order_number=None)

    async def owns_only_1001(customer_id: UUID, order_number: str) -> bool:
        return order_number == "ORD-1001"

    fake = AgentFake(scripts)
    deps = fake_deps(scripted_client(fake), world, owns_order=owns_only_1001)
    saver = memory_checkpointer(deps)
    first = await start_workflow(deps, saver, thread_for(state), state)
    assert first.paused
    assert first.state.get("asked_for") == AskedFor.ORDER_NUMBER
    assert first.state["status"] == RunStatus.AWAITING_CUSTOMER

    second = await resume_workflow(deps, saver, thread_for(state), {"text": "It is order 1001."})

    assert second.paused
    assert second.state["status"] == RunStatus.AWAITING_APPROVAL
    assert second.state.get("order_number") == "ORD-1001"
    assert second.state.get("clarification_rounds") == 1
    # The reply reached the classifier together with the original complaint.
    first_text, second_text = classifier_texts(fake)
    assert "It is order 1001." not in first_text
    assert "My refund has not arrived." in second_text and "It is order 1001." in second_text


async def test_more_information_gathers_again_with_fresh_evidence() -> None:
    scripts: dict[str, list[ScriptStep]] = {
        "classifier": [classified("refund")],
        "supervisor": [supervisor_reply(), supervisor_reply()],
        "investigator": simple_investigation() + simple_investigation(),
        "policy": policy_run(CITATION) + policy_run(CITATION),
        "resolution": [
            draft_reply("request_info", message="Which item?", citations=[]),
            draft_reply("refund", citations=[CITATION]),
        ],
        "critic": [critique_reply("approve"), critique_reply("approve")],
    }
    world = FakeWorld()
    state = new_state()
    first, deps, fake, saver = await start(scripts, world, state)
    assert first.paused and first.state.get("asked_for") == AskedFor.MORE_INFORMATION

    second = await resume_workflow(deps, saver, thread_for(state), {"text": "The headphones."})

    assert second.state["status"] == RunStatus.AWAITING_APPROVAL
    assert len(fake.requests_by_agent["supervisor"]) == 2  # gathered again, not re-classified
    assert len(fake.requests_by_agent["classifier"]) == 1
    investigation = second.written.get("investigation")
    assert isinstance(investigation, InvestigationOutcome)  # a new pass, recorded once
    assert len(world.proposals) == 1


async def test_the_clarification_limit_escalates_instead_of_asking_again() -> None:
    scripts = {"classifier": [classified("unclear", 0.9), classified("unclear", 0.9)]}
    world = FakeWorld()
    state = new_state()
    first, deps, _, saver = await start(scripts, world, state, max_rounds=1)
    assert first.paused and first.state.get("asked_for") == AskedFor.CLARIFICATION

    second = await resume_workflow(deps, saver, thread_for(state), {"text": "still not sure"})

    assert not second.paused
    assert second.state["status"] == RunStatus.ESCALATED
    assert second.state.get("clarification_rounds") == 1


async def test_a_bad_customer_resume_value_is_refused() -> None:
    state = new_state()
    first, deps, _, saver = await start({"classifier": [classified("unclear")]}, FakeWorld(), state)
    assert first.paused

    with pytest.raises(ValueError, match="text"):
        await resume_workflow(deps, saver, thread_for(state), {"message": "hi"})


# ---------------------------------------------------------------- the serializer


class NotOurs(BaseModel):
    payload: str


def test_the_serializer_revives_our_types_and_refuses_others() -> None:
    world = FakeWorld()
    serde = checkpoint_serializer(world.registry)
    assert InvestigationOutcome in allowed_types(world.registry)

    revived = serde.loads_typed(serde.dumps_typed({"value": NotOurs(payload="x")}))

    assert not isinstance(revived["value"], NotOurs)  # left as plain data, never imported


async def test_a_full_refund_run_needs_no_type_outside_the_allowlist(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing allowlist entry would silently turn a model into a dict on resume."""
    world = FakeWorld()
    state = new_state()
    _, deps, _, saver = await start(refund_scripts(), world, state)
    world.approval_decision = ApprovalDecision.APPROVED
    await resume_workflow(deps, saver, thread_for(state), {"event": "decided"})

    blocked = [r for r in caplog.records if "deserializ" in r.getMessage().lower()]
    assert blocked == []
