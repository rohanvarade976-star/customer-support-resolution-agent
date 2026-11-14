"""The resolution loop: draft -> validate -> critique -> revise -> final validation.

Fake LLM only. The investigator's trace and results are built by hand, so no tools run.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from support_agent.agents.fake import critique_reply, draft_reply
from support_agent.agents.investigator import Evidence, Fact
from support_agent.agents.policy import PolicyFindings, PolicyRuleDraft
from support_agent.agents.resolution import ResolutionInputs
from support_agent.agents.resolve import (
    ESCALATION_MESSAGE,
    EscalationReason,
    ResolutionFailure,
    ResolutionOutcome,
    resolve,
)
from support_agent.domain.eligibility import EligibilityDecision, ReasonCode
from support_agent.domain.enums import OrderStatus, ResolutionAction
from support_agent.domain.shipping import compute_delay_facts
from support_agent.domain.validation import ValidationCode
from support_agent.integrations.shipping import Shipment
from support_agent.llm.client import RetrySettings, RoutedLLMClient
from support_agent.llm.fake import FakeProvider, ScriptStep, text_reply, transient
from support_agent.llm.tool_loop import OUTCOME_OK, ToolCallRecord, ToolResult
from support_agent.schemas.orders import OrderResponse

ORDER = "ORD-1001"
CITATION = "refund_policy_v2:section_3"
ELIGIBLE_MINOR = 129900
PROPOSED = "Your return was received, so a refund is proposed and needs approval."
OVERCLAIM = "Great news, your refund has been issued."

DELIVERED = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def order_result(delivered_at: datetime | None) -> ToolResult:
    order = OrderResponse(
        order_number=ORDER,
        status=OrderStatus.DELIVERED,
        currency="INR",
        paid_amount_minor=ELIGIBLE_MINOR,
        placed_at=DELIVERED - timedelta(days=5),
        delivered_at=delivered_at,
        items=[],
        returns=[],
    )
    return ToolResult("call_1", "get_order", {"order_number": ORDER}, order)


def shipment_result(delivered_at: datetime | None) -> ToolResult:
    shipment = Shipment(
        order_reference=ORDER,
        status="delivered",
        carrier="TestCarrier",
        tracking_number="TRK1",
        shipped_at=DELIVERED - timedelta(days=3),
        expected_delivery=date(2026, 11, 1),
        delivered_at=delivered_at,
        events=[],
    )
    return ToolResult("call_2", "get_shipment", {"order_number": ORDER}, shipment)


def trace_of(*call_ids: str) -> list[ToolCallRecord]:
    return [
        ToolCallRecord(call_id=cid, tool="get_order", outcome=OUTCOME_OK, latency_ms=1)
        for cid in call_ids
    ]


def inputs(eligible: bool = True) -> ResolutionInputs:
    decision = (
        EligibilityDecision(eligible=True, refundable_amount_minor=ELIGIBLE_MINOR, reasons=[])
        if eligible
        else EligibilityDecision(
            eligible=False, refundable_amount_minor=0, reasons=[ReasonCode.OUTSIDE_RETURN_WINDOW]
        )
    )
    rule = PolicyRuleDraft(statement="Returns are accepted within 30 days.", citation=CITATION)
    return ResolutionInputs(
        complaint_text="I returned my headphones. SECRET-COMPLAINT-TEXT",
        order_number=ORDER,
        evidence=Evidence(
            facts=[Fact(statement="The order was delivered.", call_id="call_1")],
            dropped_facts=[],
            missing_information=[],
            plan_changes=[],
        ),
        findings=PolicyFindings(
            rules=[rule],
            dropped_rules=[],
            policy_versions=["refund_policy v2"],
            no_relevant_policy=False,
            conflicting_passages=False,
            ungrounded_citations=False,
        ),
        eligibility=decision,
    )


async def run(
    script: Sequence[ScriptStep],
    *,
    case: ResolutionInputs | None = None,
    results: list[ToolResult] | None = None,
    max_revisions: int = 1,
    critic_enabled: bool = True,
) -> tuple[ResolutionOutcome, FakeProvider]:
    fake = FakeProvider(list(script))
    client = RoutedLLMClient(
        [fake],
        RetrySettings(max_attempts=1, backoff_base_seconds=0, backoff_max_seconds=0),
        default_temperature=0.0,
        default_max_tokens=256,
    )
    outcome = await resolve(
        client,
        case or inputs(),
        trace=trace_of("call_1", "call_2"),
        results=results if results is not None else [order_result(DELIVERED)],
        max_revisions=max_revisions,
        delivery_tolerance=timedelta(hours=24),
        critic_tier="large",
        critic_enabled=critic_enabled,
    )
    return outcome, fake


def good_draft() -> ScriptStep:
    return draft_reply("refund", message=PROPOSED, citations=[CITATION])


# ---------------------------------------------------------------- the happy path


async def test_priya_style_case_reaches_a_valid_refund_draft() -> None:
    outcome, fake = await run([good_draft(), critique_reply("approve")])

    assert outcome.action == ResolutionAction.REFUND
    assert outcome.amount_minor == ELIGIBLE_MINOR
    assert outcome.message == PROPOSED
    assert outcome.reason_codes == []
    assert outcome.failure is None
    assert outcome.draft is not None and outcome.draft.policy_citations == [CITATION]
    assert [r.tier for r in fake.requests] == ["large", "large"]
    assert [r.forced_tool for r in fake.requests] == ["resolution_draft", "critic_verdict"]


async def test_untrusted_text_is_marked_as_data_and_never_in_the_system_message() -> None:
    _, fake = await run([good_draft(), critique_reply("approve")])

    for request in fake.requests:
        assert "SECRET-COMPLAINT-TEXT" not in request.messages[0].content
        assert 'trust="untrusted_data"' in request.messages[1].content


# ---------------------------------------------------------------- amounts come from code


async def test_an_amount_supplied_by_the_model_is_ignored() -> None:
    script = [
        draft_reply("refund", message=PROPOSED, citations=[CITATION], amount_minor=999999),
        critique_reply("approve"),
    ]

    outcome, _ = await run(script)

    assert outcome.action == ResolutionAction.REFUND
    assert outcome.amount_minor == ELIGIBLE_MINOR


async def test_a_refund_draft_for_an_ineligible_order_is_escalated_not_refunded() -> None:
    script = [good_draft(), good_draft()]  # the model insists on a refund, twice

    outcome, _ = await run(script, case=inputs(eligible=False))

    assert outcome.action == ResolutionAction.ESCALATE
    assert outcome.amount_minor is None
    assert ValidationCode.REFUND_NOT_ELIGIBLE.value in outcome.reason_codes
    assert EscalationReason.REVISIONS_EXHAUSTED.value in outcome.reason_codes


# ---------------------------------------------------------------- citations


async def test_a_nonexistent_call_id_is_rejected_and_can_be_revised() -> None:
    script = [
        draft_reply("refund", message=PROPOSED, citations=[CITATION], call_ids=["call_99"]),
        good_draft(),
        critique_reply("approve"),
    ]

    outcome, fake = await run(script)

    assert outcome.action == ResolutionAction.REFUND
    assert outcome.attempts[0].validation == [ValidationCode.CITED_CALL_NOT_FOUND]
    assert outcome.attempts[0].verdict is None  # the critic only sees drafts that validated
    assert len(fake.requests) == 3


async def test_an_invented_policy_citation_is_rejected() -> None:
    script = [
        draft_reply("refund", message=PROPOSED, citations=["invented:section_1"]),
        draft_reply("refund", message=PROPOSED, citations=["invented:section_1"]),
    ]

    outcome, _ = await run(script)

    assert outcome.action == ResolutionAction.ESCALATE
    assert ValidationCode.CITATION_NOT_GROUNDED.value in outcome.reason_codes


# ---------------------------------------------------------------- conflicts


async def test_meera_style_date_conflict_forces_escalate_without_revision() -> None:
    results = [
        order_result(DELIVERED),
        shipment_result(DELIVERED + timedelta(days=4)),
    ]

    outcome, fake = await run([good_draft()], results=results)

    assert outcome.action == ResolutionAction.ESCALATE
    assert outcome.amount_minor is None
    assert outcome.reason_codes == [ValidationCode.EVIDENCE_CONFLICT.value]
    assert outcome.message == ESCALATION_MESSAGE
    assert len(fake.requests) == 1  # no critic, no revision: a rewrite cannot fix the data


async def test_matching_dates_do_not_block_a_refund() -> None:
    results = [order_result(DELIVERED), shipment_result(DELIVERED + timedelta(hours=2))]

    outcome, _ = await run([good_draft(), critique_reply("approve")], results=results)

    assert outcome.action == ResolutionAction.REFUND


# ---------------------------------------------------------------- the message


async def test_an_overclaiming_message_is_rejected_then_corrected() -> None:
    script = [
        draft_reply("refund", message=OVERCLAIM, citations=[CITATION]),
        good_draft(),
        critique_reply("approve"),
    ]

    outcome, fake = await run(script)

    assert outcome.attempts[0].validation == [ValidationCode.UNVERIFIED_CLAIM]
    assert outcome.message == PROPOSED
    # The revision request names the failed check, in our words, and shows the old draft.
    revision = fake.requests[1].messages
    assert "UNVERIFIED_CLAIM" in revision[-1].content
    assert OVERCLAIM in revision[-2].content


async def test_an_overclaiming_message_that_is_never_fixed_escalates() -> None:
    bad = draft_reply("refund", message=OVERCLAIM, citations=[CITATION])

    outcome, _ = await run([bad, bad])

    assert outcome.action == ResolutionAction.ESCALATE
    assert outcome.message == ESCALATION_MESSAGE  # the rejected draft is never shown
    assert outcome.reason_codes == [
        ValidationCode.UNVERIFIED_CLAIM.value,
        EscalationReason.REVISIONS_EXHAUSTED.value,
    ]


# ---------------------------------------------------------------- the critic


async def test_the_critic_can_send_a_draft_back_once_and_it_then_passes() -> None:
    script = [
        good_draft(),
        critique_reply("revise", ["The message is too long."]),
        good_draft(),
        critique_reply("approve"),
    ]

    outcome, fake = await run(script)

    assert outcome.action == ResolutionAction.REFUND
    assert len(outcome.attempts) == 2
    # The critic's words reach the drafter only as marked, untrusted data.
    assert 'trust="untrusted_data"' in fake.requests[2].messages[-1].content
    assert "too long" in fake.requests[2].messages[-1].content


async def test_the_revise_loop_stops_at_the_limit_and_escalates() -> None:
    script = [
        good_draft(),
        critique_reply("revise", ["Issue A."]),
        good_draft(),
        critique_reply("revise", ["Issue B."]),
        good_draft(),  # never reached with max_revisions=1
    ]

    outcome, fake = await run(script, max_revisions=1)

    assert outcome.action == ResolutionAction.ESCALATE
    assert outcome.reason_codes == [
        EscalationReason.CRITIC_OBJECTED.value,
        EscalationReason.REVISIONS_EXHAUSTED.value,
    ]
    assert len(outcome.attempts) == 2
    assert len(fake.requests) == 4


async def test_two_revisions_are_allowed_when_configured() -> None:
    script = [
        good_draft(),
        critique_reply("revise", ["A."]),
        good_draft(),
        critique_reply("revise", ["B."]),
        good_draft(),
        critique_reply("approve"),
    ]

    outcome, _ = await run(script, max_revisions=2)

    assert outcome.action == ResolutionAction.REFUND
    assert len(outcome.attempts) == 3


async def test_zero_revisions_escalates_on_the_first_objection() -> None:
    outcome, fake = await run([good_draft(), critique_reply("revise", ["A."])], max_revisions=0)

    assert outcome.action == ResolutionAction.ESCALATE
    assert len(fake.requests) == 2


async def test_critic_approval_cannot_override_a_validator_failure() -> None:
    # The critic approves everything it is asked, but the draft overclaims every time.
    bad = draft_reply("refund", message=OVERCLAIM, citations=[CITATION])
    script = [bad, bad]  # a critic reply is never needed: the validator stops each draft first

    outcome, fake = await run(script, max_revisions=1)

    assert outcome.action == ResolutionAction.ESCALATE
    assert ValidationCode.UNVERIFIED_CLAIM.value in outcome.reason_codes
    # The critic never even saw the failing drafts: the validator runs first.
    assert all(attempt.verdict is None for attempt in outcome.attempts)
    assert [r.forced_tool for r in fake.requests] == ["resolution_draft", "resolution_draft"]


async def test_a_critic_cannot_change_the_amount() -> None:
    script = [
        good_draft(),
        # Extra fields in the critic's answer are not part of its schema and have no effect.
        critique_reply("approve"),
    ]

    outcome, _ = await run(script)

    assert outcome.amount_minor == ELIGIBLE_MINOR


async def test_with_the_critic_off_a_valid_draft_is_accepted_without_a_critic_call() -> None:
    outcome, fake = await run([good_draft()], critic_enabled=False)

    assert outcome.action == ResolutionAction.REFUND
    assert outcome.amount_minor == ELIGIBLE_MINOR
    assert outcome.attempts[0].verdict is None
    assert [r.forced_tool for r in fake.requests] == ["resolution_draft"]


async def test_with_the_critic_off_the_validator_still_rejects_and_revises() -> None:
    overclaim = draft_reply("refund", message=OVERCLAIM, citations=[CITATION])

    outcome, fake = await run([overclaim, good_draft()], critic_enabled=False)

    assert ValidationCode.UNVERIFIED_CLAIM in outcome.attempts[0].validation
    assert outcome.action == ResolutionAction.REFUND
    assert outcome.message == PROPOSED
    assert [r.forced_tool for r in fake.requests] == ["resolution_draft", "resolution_draft"]


# ---------------------------------------------------------------- other actions and failures


async def test_a_draft_that_escalates_on_its_own_is_accepted_without_the_critic() -> None:
    escalate = draft_reply("escalate", message="A person will look at this.", call_ids=[])

    outcome, fake = await run([escalate])

    assert outcome.action == ResolutionAction.ESCALATE
    assert outcome.reason_codes == [EscalationReason.MODEL_ESCALATED.value]
    assert len(fake.requests) == 1


async def test_a_llm_outage_escalates_with_the_failure_set() -> None:
    outcome, _ = await run([transient()])

    assert outcome.action == ResolutionAction.ESCALATE
    assert outcome.failure == ResolutionFailure.LLM_UNAVAILABLE
    assert outcome.reason_codes == ["LLM_UNAVAILABLE"]


async def test_an_unreadable_draft_escalates_with_the_failure_set() -> None:
    # Not a tool call at all, twice: the structured call repairs once and then gives up.
    outcome, _ = await run([text_reply("no"), text_reply("still no")])

    assert outcome.action == ResolutionAction.ESCALATE
    assert outcome.failure == ResolutionFailure.RESOLUTION_OUTPUT_INVALID


async def test_an_unreadable_critic_answer_escalates_instead_of_approving() -> None:
    outcome, _ = await run([good_draft(), text_reply("no"), text_reply("still no")])

    assert outcome.action == ResolutionAction.ESCALATE
    assert outcome.failure == ResolutionFailure.CRITIC_OUTPUT_INVALID


# ---------------------------------------------------------------- regression: live-run findings


@pytest.mark.parametrize(
    "wrapped",
    [f"[{CITATION}]", f'"{CITATION}"', f"  {CITATION} "],
)
async def test_a_wrapped_citation_is_normalized_and_accepted(wrapped: str) -> None:
    # Rahul's live run: the model wrote "[refund_policy_v2:section_3]" and was escalated.
    script = [
        draft_reply("refund", message=PROPOSED, citations=[wrapped]),
        critique_reply("approve"),
    ]

    outcome, _ = await run(script)

    assert outcome.action == ResolutionAction.REFUND
    assert outcome.reason_codes == []
    assert outcome.draft is not None and outcome.draft.policy_citations == [CITATION]


async def test_a_wrapped_call_id_is_normalized_too() -> None:
    script = [
        draft_reply("refund", message=PROPOSED, citations=[CITATION], call_ids=["[call_1]"]),
        critique_reply("approve"),
    ]

    outcome, _ = await run(script)

    assert outcome.action == ResolutionAction.REFUND


async def test_the_revision_feedback_names_the_exact_allowed_citations() -> None:
    script = [
        draft_reply("refund", message=PROPOSED, citations=["invented:section_1"]),
        good_draft(),
        critique_reply("approve"),
    ]

    outcome, fake = await run(script)

    assert outcome.action == ResolutionAction.REFUND
    feedback = fake.requests[1].messages[-1].content
    assert "CITATION_NOT_GROUNDED" in feedback
    assert f"- {CITATION}" in feedback
    assert "exact" in feedback


async def test_the_case_text_shows_citations_without_brackets() -> None:
    _, fake = await run([good_draft(), critique_reply("approve")])

    case = fake.requests[0].messages[1].content
    assert f"citation: {CITATION}" in case
    assert f"[{CITATION}]" not in case


ARJUN_OVERCLAIM = (
    "Your laptop is over 7 days past the expected delivery date. An investigation will be started."
)
ARJUN_HONEST = "Your laptop is 3 days past its expected delivery date and is still in transit."


def arjun_case() -> ResolutionInputs:
    facts = compute_delay_facts(
        expected_delivery=date(2026, 11, 10),
        delivered_at=None,
        carrier_status="in_transit",
        now=datetime(2026, 11, 13, 12, 0, tzinfo=UTC),
        threshold_days=7,
    )
    return replace(inputs(eligible=False), delay_facts=facts)


async def test_the_delay_facts_reach_the_drafter_as_system_computed_lines() -> None:
    _, fake = await run(
        [draft_reply("explain_status", message=ARJUN_HONEST), critique_reply()], case=arjun_case()
    )

    case = fake.requests[0].messages[1].content
    assert "days past the expected delivery date: 3" in case
    assert "no investigation applies" in case


async def test_arjuns_false_delay_and_promised_investigation_are_corrected_on_revision() -> None:
    script = [
        draft_reply("explain_status", message=ARJUN_OVERCLAIM),
        draft_reply("explain_status", message=ARJUN_HONEST),
        critique_reply("approve"),
    ]

    outcome, _ = await run(script, case=arjun_case())

    assert outcome.attempts[0].validation == [
        ValidationCode.UNVERIFIED_CLAIM,
        ValidationCode.UNSUPPORTED_PROMISE,
    ]
    assert outcome.action == ResolutionAction.EXPLAIN_STATUS
    assert outcome.message == ARJUN_HONEST


async def test_a_draft_that_keeps_the_false_claims_escalates() -> None:
    bad = draft_reply("explain_status", message=ARJUN_OVERCLAIM)

    outcome, _ = await run([bad, bad], case=arjun_case())

    assert outcome.action == ResolutionAction.ESCALATE
    assert outcome.message == ESCALATION_MESSAGE
    assert ValidationCode.UNSUPPORTED_PROMISE.value in outcome.reason_codes
