"""The pure rules behind a run's live status (domain/run_events.py, ADR-019)."""

from support_agent.domain.enums import RunStatus, TicketStatus
from support_agent.domain.run_events import (
    is_finished,
    reviewer_details,
    safe_actor,
    sse_comment,
    sse_frame,
    visible_to_customer,
)


def test_customers_see_customer_events_only() -> None:
    assert visible_to_customer("approval.requested")
    assert visible_to_customer("refund.succeeded")
    assert not visible_to_customer("tool.called")
    assert not visible_to_customer("run.usage")
    assert not visible_to_customer("injection_suspected")


def test_reviewer_details_keep_allowlisted_codes_only() -> None:
    payload = {
        "approval_id": "0b6f3c1e-1111-4a2b-8c3d-123456789abc",
        "amount_minor": 249900,
        "codes": ["FAKE_SYSTEM", "IGNORE_RULES"],
        "note": "Customer seems honest",  # reviewer free text: key not allowed
        "reviewer_id": "staff-1",  # identity: key not allowed
        "reason": "the customer said something long",  # allowed key, but a sentence
        "facts": {"nested": "dict"},  # nested values are dropped
    }
    assert reviewer_details(payload) == {
        "amount_minor": 249900,
        "approval_id": "0b6f3c1e-1111-4a2b-8c3d-123456789abc",
        "codes": ["FAKE_SYSTEM", "IGNORE_RULES"],
    }


def test_sentences_inside_lists_are_dropped() -> None:
    assert reviewer_details({"reasons": ["NO_RETURN", "a whole sentence here"]}) == {
        "reasons": ["NO_RETURN"]
    }


def test_timestamps_and_citations_pass() -> None:
    details = reviewer_details(
        {"expires_at": "2026-11-10T13:00:00+00:00", "reason": "refund_policy_v2:section_3"}
    )
    assert details == {
        "expires_at": "2026-11-10T13:00:00+00:00",
        "reason": "refund_policy_v2:section_3",
    }


def test_actor_must_be_a_code() -> None:
    assert safe_actor("reviewer") == "reviewer"
    assert safe_actor("Anita Sharma") == "other"


def test_finished_waits_for_a_refund_in_flight() -> None:
    assert is_finished(RunStatus.COMPLETED, TicketStatus.RESOLVED)
    assert is_finished(RunStatus.ESCALATED, TicketStatus.ESCALATED)
    assert not is_finished(RunStatus.COMPLETED, TicketStatus.IN_PROGRESS)  # refund on its way
    assert not is_finished(RunStatus.AWAITING_APPROVAL, TicketStatus.IN_PROGRESS)


def test_sse_frames() -> None:
    assert sse_frame("step", '{"a":1}', event_id="x") == 'id: x\nevent: step\ndata: {"a":1}\n\n'
    assert sse_frame("end", "{}") == "event: end\ndata: {}\n\n"
    assert sse_comment("keep-alive") == ": keep-alive\n\n"
