"""The escalations inbox and its actions (ADR-022), checked against database state.

- listing: escalated tickets with code reasons, newest first; reviewers only (403 for customers);
- reply: guardrail refusals store nothing; an accepted reply is the ticket's verified reply,
  marked "specialist", audited with no text, and shown to the customer;
- resolve: needs a specialist reply first; the note stays in the audit trail and never
  reaches a response;
- a manual refund proposal still needs an approval, moves no money, and the reviewer who
  proposed it cannot decide it (separation of duties). Another reviewer can.

The LLM is the scripted AgentFake; everything else is real.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mock_providers.payments.testing import InMemoryPayments
from support_agent.agents.fake import (
    AgentFake,
    critique_reply,
    draft_reply,
    policy_run,
    simple_investigation,
    supervisor_reply,
)
from support_agent.agents.resolve import ESCALATION_MESSAGE
from support_agent.api.deps import get_clock, get_payment_client
from support_agent.core.config import Settings, get_settings
from support_agent.db.models import (
    ActionProposal,
    Approval,
    AuditEvent,
    Customer,
    RefundOperation,
    StaffUser,
    Ticket,
)
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import ApprovalDecision, ReplyAuthor, TicketStatus
from support_agent.integrations.payments import PaymentClient
from support_agent.llm.fake import ScriptStep, tool_reply
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.services import policy_search

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)  # the mock shipping provider's instant
ANITA = "anita@example.com"
KARAN = "karan@example.com"
PRIYA = "priya@example.com"
NOTE = "ZZNOTEMARKER41 called the warehouse, all fine"
REPLY = "We checked your return with the warehouse. Your refund will be reviewed today."
HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseAgents = Callable[[dict[str, list[ScriptStep]]], AgentFake]
Drain = Callable[[], Awaitable[int]]


@pytest.fixture(autouse=True)
def wire_app(app: FastAPI, payments: InMemoryPayments) -> Settings:
    app.dependency_overrides[get_clock] = lambda: lambda: NOW

    async def payment_client() -> AsyncIterator[PaymentClient]:
        async with payments.client() as client:
            yield client

    app.dependency_overrides[get_payment_client] = payment_client
    settings = get_settings().model_copy(update={"policy_min_similarity": -1.0})
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str, ingested_policies: None) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


@pytest.fixture
async def cited(session: AsyncSession, wire_app: Settings) -> str:
    priya_id = await session.scalar(select(Customer.id).where(Customer.email == PRIYA))
    assert priya_id is not None
    found = await policy_search.search_policy(
        session, FakeEmbedder(), wire_app, priya_id, "return window", "ORD-1001", NOW
    )
    return found.passages[0].citation


@pytest.fixture
async def escalated(
    client: httpx.AsyncClient,
    use_agents: UseAgents,
    auth_headers: HeadersFor,
    drain_jobs: Drain,
    cited: str,
) -> tuple[UUID, UUID]:
    """Priya's run, where the AI itself chose to escalate: (ticket id, run id)."""
    classified = {"category": "refund", "confidence": 0.95, "reason": "t"}
    use_agents(
        {
            "classifier": [tool_reply("classification", classified)],
            "supervisor": [supervisor_reply()],
            "policy": policy_run(cited),
            "investigator": simple_investigation("ORD-1001"),
            "resolution": [draft_reply("escalate", message="A person will look at this.")],
            "critic": [critique_reply("approve")],
        }
    )
    headers = await auth_headers(PRIYA)
    body = {"message": "I want to speak to someone", "order_number": "ORD-1001"}
    created = await client.post("/tickets", json=body, headers=headers)
    started = await client.post(f"/tickets/{created.json()['id']}/runs", headers=headers)
    assert started.status_code == 202
    await drain_jobs()
    return UUID(created.json()["id"]), UUID(started.json()["run_id"])


async def _ticket(session: AsyncSession, ticket_id: UUID) -> Ticket:
    ticket = await session.get(Ticket, ticket_id, populate_existing=True)
    assert ticket is not None
    return ticket


async def _events(session: AsyncSession, run_id: UUID, event_type: str) -> list[AuditEvent]:
    stmt = select(AuditEvent).where(
        AuditEvent.run_id == run_id, AuditEvent.event_type == event_type
    )
    return list((await session.scalars(stmt)).all())


async def _staff_id(session: AsyncSession, email: str) -> UUID:
    found = await session.scalar(select(StaffUser.id).where(StaffUser.email == email))
    assert found is not None
    return found


async def _reply(
    client: httpx.AsyncClient, ticket_id: UUID, headers: dict[str, str], text: str = REPLY
) -> httpx.Response:
    return await client.post(
        f"/reviewer/escalations/{ticket_id}/reply", json={"text": text}, headers=headers
    )


# ---------------------------------------------------------------- listing and access


async def test_the_inbox_lists_the_escalated_ticket_with_code_reasons(
    client: httpx.AsyncClient, auth_headers: HeadersFor, escalated: tuple[UUID, UUID]
) -> None:
    ticket_id, run_id = escalated
    response = await client.get("/reviewer/escalations", headers=await auth_headers(ANITA))
    assert response.status_code == 200
    item = next(i for i in response.json()["items"] if i["ticket_id"] == str(ticket_id))
    assert item["run_id"] == str(run_id)
    assert item["customer_name"] == "Priya Sharma"
    assert item["order_number"] == "ORD-1001"
    assert item["reasons"] == [{"code": "AI_CHOSE_TO_ESCALATE", "codes": []}]
    assert item["specialist_replied"] is False

    detail = await client.get(
        f"/reviewer/escalations/{ticket_id}", headers=await auth_headers(ANITA)
    )
    assert detail.status_code == 200
    case = detail.json()
    assert case["reply"] == ESCALATION_MESSAGE
    assert case["reply_from"] == "assistant"
    assert case["facts"]["eligible_now"] is True


async def test_customers_cannot_use_the_inbox_or_its_actions(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    escalated: tuple[UUID, UUID],
) -> None:
    ticket_id, _run_id = escalated
    priya = await auth_headers(PRIYA)  # the ticket's own customer
    base = f"/reviewer/escalations/{ticket_id}"
    assert (await client.get("/reviewer/escalations", headers=priya)).status_code == 403
    assert (await client.get(base, headers=priya)).status_code == 403
    assert (await _reply(client, ticket_id, priya)).status_code == 403
    resolved = await client.post(f"{base}/resolve", json={"note": "x"}, headers=priya)
    assert resolved.status_code == 403
    assert (await client.get("/reviewer/escalations")).status_code == 401

    ticket = await _ticket(session, ticket_id)  # nothing changed
    assert (ticket.status, ticket.reply_author) == (TicketStatus.ESCALATED, ReplyAuthor.ASSISTANT)


async def test_an_unknown_ticket_is_404(
    client: httpx.AsyncClient, auth_headers: HeadersFor
) -> None:
    response = await client.get(
        f"/reviewer/escalations/{UUID(int=5)}", headers=await auth_headers(ANITA)
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "ESCALATION_NOT_FOUND"


# ---------------------------------------------------------------- reply


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Your case 3f2b8c1e-1d2a-4b5c-9e8f-0a1b2c3d4e5f is closed.", "REPLY_INTERNAL_ID"),
        ("Email me at someone@example.com and I will help.", "REPLY_CONTACT_DETAILS"),
    ],
)
async def test_a_reply_that_fails_the_guardrail_is_refused_and_nothing_is_stored(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    escalated: tuple[UUID, UUID],
    text: str,
    code: str,
) -> None:
    ticket_id, run_id = escalated
    response = await _reply(client, ticket_id, await auth_headers(ANITA), text)
    assert response.status_code == 422
    assert response.json()["error_code"] == "REPLY_REJECTED"
    assert code in response.json()["details"]["codes"]

    ticket = await _ticket(session, ticket_id)
    assert ticket.customer_reply == ESCALATION_MESSAGE
    assert ticket.reply_author == ReplyAuthor.ASSISTANT
    assert await _events(session, run_id, "ticket.specialist_replied") == []


async def test_a_reply_is_stored_audited_without_text_and_shown_to_the_customer(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    escalated: tuple[UUID, UUID],
) -> None:
    ticket_id, run_id = escalated
    response = await _reply(client, ticket_id, await auth_headers(ANITA))
    assert response.status_code == 200
    assert response.json() == {
        "ticket_id": str(ticket_id),
        "ticket_status": "escalated",
        "reply_from": "specialist",
    }

    ticket = await _ticket(session, ticket_id)
    assert ticket.customer_reply == REPLY
    assert (ticket.status, ticket.reply_author) == (TicketStatus.ESCALATED, ReplyAuthor.SPECIALIST)
    [event] = await _events(session, run_id, "ticket.specialist_replied")
    assert event.actor == "reviewer"
    assert event.redacted_payload["reviewer_id"] == str(await _staff_id(session, ANITA))
    assert event.redacted_payload["reply_length"] == len(REPLY)
    assert REPLY not in str(event.redacted_payload)

    priya = await auth_headers(PRIYA)
    shown = (await client.get(f"/tickets/{ticket_id}", headers=priya)).json()
    assert (shown["reply"], shown["reply_from"]) == (REPLY, "specialist")
    snapshot = (await client.get(f"/runs/{run_id}/events", headers=priya)).json()
    assert (snapshot["run"]["reply"], snapshot["run"]["reply_from"]) == (REPLY, "specialist")
    assert "ticket.specialist_replied" in [step["type"] for step in snapshot["steps"]]


# ---------------------------------------------------------------- resolve


async def test_resolving_needs_a_specialist_reply_first(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    escalated: tuple[UUID, UUID],
) -> None:
    ticket_id, _run_id = escalated
    response = await client.post(
        f"/reviewer/escalations/{ticket_id}/resolve",
        json={"note": NOTE},
        headers=await auth_headers(ANITA),
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "REPLY_REQUIRED"
    assert (await _ticket(session, ticket_id)).status == TicketStatus.ESCALATED


async def test_resolving_closes_the_ticket_and_keeps_the_note_in_the_audit_trail_only(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    escalated: tuple[UUID, UUID],
) -> None:
    ticket_id, run_id = escalated
    anita = await auth_headers(ANITA)
    assert (await _reply(client, ticket_id, anita)).status_code == 200
    url = f"/reviewer/escalations/{ticket_id}/resolve"
    response = await client.post(url, json={"note": NOTE}, headers=anita)
    assert response.status_code == 200
    assert response.json()["ticket_status"] == "resolved"

    assert (await _ticket(session, ticket_id)).status == TicketStatus.RESOLVED
    [event] = await _events(session, run_id, "ticket.resolved")
    assert event.redacted_payload["note"] == NOTE
    # Never in any response: not the reviewer's timeline, not the customer's, not the inbox.
    for email in (ANITA, PRIYA):
        snapshot = await client.get(f"/runs/{run_id}/events", headers=await auth_headers(email))
        assert NOTE not in snapshot.text
    listed = await client.get("/reviewer/escalations", headers=anita)
    assert str(ticket_id) not in listed.text

    again = await client.post(url, json={"note": NOTE}, headers=anita)
    assert again.status_code == 409
    assert again.json()["error_code"] == "TICKET_NOT_ESCALATED"


# ---------------------------------------------------------------- manual proposal


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def test_a_manual_proposal_needs_another_reviewer_to_decide_it(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    escalated: tuple[UUID, UUID],
) -> None:
    ticket_id, _run_id = escalated
    anita, karan = await auth_headers(ANITA), await auth_headers(KARAN)

    proposed = await client.post(f"/tickets/{ticket_id}/refund-proposals", headers=anita)
    assert proposed.status_code == 201
    approval_id = UUID(proposed.json()["approval_id"])
    proposal = await session.get(ActionProposal, UUID(proposed.json()["proposal_id"]))
    assert proposal is not None
    assert proposal.proposed_by == await _staff_id(session, ANITA)
    # Still only a proposal: a pending approval, no refund, and the inbox has moved it on.
    approval = await session.get(Approval, approval_id, populate_existing=True)
    assert approval is not None and approval.decision == ApprovalDecision.PENDING
    assert await _count(session, RefundOperation, RefundOperation.proposal_id == proposal.id) == 0
    listed = await client.get("/reviewer/escalations", headers=anita)
    assert str(ticket_id) not in listed.text

    # Needs attention: a person proposed it, so no AI checks ran on it.
    case = (await client.get(f"/approvals/{approval_id}", headers=anita)).json()
    assert case["readiness"]["status"] == "needs_attention"
    assert [r["code"] for r in case["readiness"]["reasons"]] == ["REVIEWER_PROPOSED"]
    assert (case["proposed_by_reviewer"], case["proposed_by_you"]) == (True, True)
    other = (await client.get(f"/approvals/{approval_id}", headers=karan)).json()
    assert other["proposed_by_you"] is False

    # The proposer may not decide it, either way. Nothing changes; the attempt is audited.
    for decision in ("approve", "reject"):
        refused = await client.post(
            f"/approvals/{approval_id}/decision", json={"decision": decision}, headers=anita
        )
        assert refused.status_code == 409
        assert refused.json()["error_code"] == "SELF_DECISION_FORBIDDEN"
    approval = await session.get(Approval, approval_id, populate_existing=True)
    assert approval is not None and approval.decision == ApprovalDecision.PENDING
    assert await _count(session, RefundOperation, RefundOperation.proposal_id == proposal.id) == 0
    refusals = await _events(session, proposal.run_id, "approval.decision_refused")
    assert [e.redacted_payload["error_code"] for e in refusals] == ["SELF_DECISION_FORBIDDEN"] * 2

    # Another reviewer can.
    approved = await client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}, headers=karan
    )
    assert approved.status_code == 200
    approval = await session.get(Approval, approval_id, populate_existing=True)
    assert approval is not None and approval.decision == ApprovalDecision.APPROVED
    assert await _count(session, RefundOperation, RefundOperation.proposal_id == proposal.id) == 1
