"""Durable pause and resume (M7) through the API, on the real Postgres checkpointer.

    customer starts a run -> it pauses (awaiting approval / awaiting the customer)
    reviewer decides, or the customer replies -> the SAME run resumes from its checkpoint

Each request only queues work (202); `drain` plays the background worker after it.

The LLM is the scripted `AgentFake`. Everything else is real: the checkpointer (on the test
database), the tools, eligibility, the approval endpoint, the refund executor and the mock
payment provider. Money is checked by counting rows in refund_operations and at the
provider, never by reading response text.
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text
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
from support_agent.api.deps import get_checkpointer, get_clock, get_payment_client
from support_agent.core.config import Settings, get_settings
from support_agent.db.models import (
    ActionProposal,
    Approval,
    AuditEvent,
    Customer,
    Job,
    RefundOperation,
    StaffUser,
    Ticket,
    WorkflowRun,
)
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import (
    ApprovalDecision,
    JobStatus,
    JobType,
    ProposalStatus,
    RunStatus,
    TicketStatus,
)
from support_agent.integrations.payments import PaymentClient
from support_agent.llm.fake import ScriptStep, tool_reply
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.services import policy_search
from support_agent.tools.read_tools import build_read_registry
from support_agent.worker import queue
from support_agent.workflow.checkpointer import open_checkpointer
from support_agent.workflow.nodes.approval import DECLINED_MESSAGE

# Must equal the instant the mock shipping provider runs at (conftest.py).
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PRIYA_AMOUNT = 249900
REVIEWER = "anita@example.com"
HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseAgents = Callable[[dict[str, list[ScriptStep]]], AgentFake]
Drain = Callable[[], Awaitable[int]]


class MovableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture(autouse=True)
def wire_app(app: FastAPI, payments: InMemoryPayments, clock: MovableClock) -> Settings:
    """The app's clock, the mock payment provider, and a policy search the fake embedder passes."""
    app.dependency_overrides[get_clock] = lambda: clock

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


# ---------------------------------------------------------------- helpers


def classified(category: str, confidence: float = 0.95) -> ScriptStep:
    return tool_reply(
        "classification", {"category": category, "confidence": confidence, "reason": "t"}
    )


async def priya_refund_scripts(
    session: AsyncSession, settings: Settings
) -> dict[str, list[ScriptStep]]:
    priya_id = await session.scalar(
        select(Customer.id).where(Customer.email == "priya@example.com")
    )
    assert priya_id is not None
    found = await policy_search.search_policy(
        session, FakeEmbedder(), settings, priya_id, "return window", "ORD-1001", NOW
    )
    citation = found.passages[0].citation
    return {
        "classifier": [classified("refund")],
        "supervisor": [supervisor_reply()],
        "investigator": simple_investigation("ORD-1001"),
        "policy": policy_run(citation),
        "resolution": [draft_reply("refund", citations=[citation])],
        "critic": [critique_reply("approve")],
    }


def arjun_explain_scripts(classifier: list[ScriptStep]) -> dict[str, list[ScriptStep]]:
    return {
        "classifier": classifier,
        "supervisor": [supervisor_reply(policy=False)],
        "investigator": simple_investigation("ORD-1002"),
        "resolution": [draft_reply("explain_status", message="Your laptop is with the carrier.")],
        "critic": [critique_reply("approve")],
    }


async def start_run(
    client: httpx.AsyncClient,
    drain: Drain,
    headers: dict[str, str],
    message: str,
    order: str | None,
) -> tuple[str, str]:
    body: dict[str, str] = {"message": message}
    if order is not None:
        body["order_number"] = order
    created = await client.post("/tickets", json=body, headers=headers)
    assert created.status_code == 201
    ticket_id = str(created.json()["id"])
    started = await client.post(f"/tickets/{ticket_id}/runs", headers=headers)
    assert started.status_code == 202
    await drain()
    return ticket_id, str(started.json()["run_id"])


async def reply(
    client: httpx.AsyncClient, drain: Drain, headers: dict[str, str], run_id: str, message: str
) -> httpx.Response:
    response = await client.post(
        f"/runs/{run_id}/messages", json={"text": message}, headers=headers
    )
    await drain()
    return response


async def decide(
    client: httpx.AsyncClient,
    drain: Drain,
    headers: dict[str, str],
    approval_id: UUID,
    decision: str,
) -> httpx.Response:
    body: dict[str, Any] = {"decision": decision, "note": "checked"}
    response = await client.post(f"/approvals/{approval_id}/decision", json=body, headers=headers)
    await drain()
    return response


async def reload[M](session: AsyncSession, model: type[M], row_id: str | UUID) -> M:
    row = await session.get(model, UUID(str(row_id)), populate_existing=True)
    assert row is not None
    return row


async def approval_for_run(session: AsyncSession, run_id: str) -> tuple[ActionProposal, Approval]:
    (proposal,) = (
        await session.scalars(select(ActionProposal).where(ActionProposal.run_id == UUID(run_id)))
    ).all()
    (approval,) = (
        await session.scalars(select(Approval).where(Approval.proposal_id == proposal.id))
    ).all()
    return proposal, approval


async def refund_rows(session: AsyncSession, run_id: str) -> int:
    """Refund operations for this run's proposals (the seed has refunds of its own)."""
    stmt = (
        select(func.count())
        .select_from(RefundOperation)
        .join(ActionProposal, ActionProposal.id == RefundOperation.proposal_id)
        .where(ActionProposal.run_id == UUID(run_id))
    )
    return int(await session.scalar(stmt) or 0)


async def events(session: AsyncSession, run_id: str, event_type: str) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(AuditEvent).where(
            AuditEvent.run_id == UUID(run_id), AuditEvent.event_type == event_type
        )
    )
    return [row.redacted_payload for row in rows]


async def all_payload_text(session: AsyncSession, run_id: str) -> str:
    rows = await session.scalars(select(AuditEvent).where(AuditEvent.run_id == UUID(run_id)))
    return json.dumps([row.redacted_payload for row in rows])


async def paused_priya_refund(
    client: httpx.AsyncClient,
    drain: Drain,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    settings: Settings,
) -> tuple[str, str]:
    use_agents(await priya_refund_scripts(session, settings))
    priya = await auth_headers("priya@example.com")
    ticket_id, run_id = await start_run(client, drain, priya, "Where is my refund?", "ORD-1001")
    run = await reload(session, WorkflowRun, run_id)
    assert run.status == RunStatus.AWAITING_APPROVAL
    return ticket_id, run_id


# ---------------------------------------------------------------- approval pause


async def test_approval_pause_then_approve_executes_exactly_one_refund(
    client: httpx.AsyncClient,
    drain_jobs: Drain,
    session: AsyncSession,
    payments: InMemoryPayments,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    ticket_id, run_id = await paused_priya_refund(
        client, drain_jobs, session, auth_headers, use_agents, wire_app
    )
    assert await refund_rows(session, run_id) == 0
    assert await events(session, run_id, "run.paused") == [
        {"run_id": run_id, "reason": "awaiting_approval"}
    ]
    proposal, approval = await approval_for_run(session, run_id)

    decided = await decide(client, drain_jobs, await auth_headers(REVIEWER), approval.id, "approve")

    assert decided.status_code == 202
    assert decided.json()["refund"] is None  # the worker executes it, after the answer
    (operation,) = (
        await session.scalars(
            select(RefundOperation).where(RefundOperation.proposal_id == proposal.id)
        )
    ).all()
    assert operation.amount_minor == PRIYA_AMOUNT
    assert operation.idempotency_key == f"refund-{proposal.id}"
    assert await payments.refund_count() == 1
    run = await reload(session, WorkflowRun, run_id)
    ticket = await reload(session, Ticket, ticket_id)
    assert run.status == RunStatus.COMPLETED
    assert ticket.status == TicketStatus.IN_PROGRESS  # accepted by the provider, not yet paid
    assert ticket.customer_reply == "The payment service accepted the refund request. " + (
        "Processing is still pending."
    )
    # Who resumed it, and nothing else.
    reviewer_id = await session.scalar(select(StaffUser.id).where(StaffUser.email == REVIEWER))
    (resumed,) = await events(session, run_id, "run.resumed")
    assert resumed == {
        "run_id": run_id,
        "reason": "awaiting_approval",
        "resumed_by": "reviewer",
        "actor_id": str(reviewer_id),
    }


async def test_reject_reopens_the_ticket_and_moves_no_money(
    client: httpx.AsyncClient,
    drain_jobs: Drain,
    session: AsyncSession,
    payments: InMemoryPayments,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    ticket_id, run_id = await paused_priya_refund(
        client, drain_jobs, session, auth_headers, use_agents, wire_app
    )
    _, approval = await approval_for_run(session, run_id)

    decided = await decide(client, drain_jobs, await auth_headers(REVIEWER), approval.id, "reject")

    assert decided.status_code == 202
    assert await refund_rows(session, run_id) == 0
    assert await payments.refund_count() == 0
    run = await reload(session, WorkflowRun, run_id)
    ticket = await reload(session, Ticket, ticket_id)
    assert run.status == RunStatus.COMPLETED
    assert ticket.status == TicketStatus.OPEN
    assert ticket.customer_reply == DECLINED_MESSAGE


async def test_an_expired_approval_escalates_and_never_executes(
    client: httpx.AsyncClient,
    drain_jobs: Drain,
    session: AsyncSession,
    payments: InMemoryPayments,
    clock: MovableClock,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    ticket_id, run_id = await paused_priya_refund(
        client, drain_jobs, session, auth_headers, use_agents, wire_app
    )
    proposal, approval = await approval_for_run(session, run_id)
    clock.now = NOW + timedelta(minutes=wire_app.approval_ttl_minutes + 1)

    decided = await decide(client, drain_jobs, await auth_headers(REVIEWER), approval.id, "approve")

    assert decided.status_code == 409
    assert decided.json()["error_code"] == "APPROVAL_EXPIRED"
    assert await refund_rows(session, run_id) == 0
    assert await payments.refund_count() == 0
    assert (await reload(session, Approval, approval.id)).decision == ApprovalDecision.EXPIRED
    assert (await reload(session, ActionProposal, proposal.id)).status == ProposalStatus.EXPIRED
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.ESCALATED
    assert (await reload(session, Ticket, ticket_id)).status == TicketStatus.ESCALATED


async def test_a_changed_proposal_never_executes(
    client: httpx.AsyncClient,
    drain_jobs: Drain,
    session: AsyncSession,
    payments: InMemoryPayments,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    _, run_id = await paused_priya_refund(
        client, drain_jobs, session, auth_headers, use_agents, wire_app
    )
    proposal, approval = await approval_for_run(session, run_id)
    # Someone edits the stored payload behind the API's back.
    await session.execute(
        text(
            "UPDATE action_proposals"
            " SET payload = jsonb_set(payload, '{amount_minor}', '999900') WHERE id = :id"
        ),
        {"id": proposal.id},
    )

    decided = await decide(client, drain_jobs, await auth_headers(REVIEWER), approval.id, "approve")

    assert decided.status_code == 409
    assert decided.json()["error_code"] == "PROPOSAL_CHANGED"
    assert await refund_rows(session, run_id) == 0
    assert await payments.refund_count() == 0
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.ESCALATED


async def test_resume_after_a_restart_continues_from_the_checkpoint(
    app: FastAPI,
    client: httpx.AsyncClient,
    drain_jobs: Drain,
    session: AsyncSession,
    payments: InMemoryPayments,
    migrated_database: str,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    _, run_id = await paused_priya_refund(
        client, drain_jobs, session, auth_headers, use_agents, wire_app
    )
    _, approval = await approval_for_run(session, run_id)

    # A "restart": a brand-new checkpointer instance with its own pool and nothing in memory.
    # Every request already builds a fresh graph, so only the saved checkpoint carries over.
    async with open_checkpointer(migrated_database, build_read_registry()) as fresh:
        app.dependency_overrides[get_checkpointer] = lambda: fresh
        decided = await decide(
            client, drain_jobs, await auth_headers(REVIEWER), approval.id, "approve"
        )

    assert decided.status_code == 202
    assert await refund_rows(session, run_id) == 1
    assert await payments.refund_count() == 1
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.COMPLETED


async def test_a_double_resume_is_refused_and_moves_no_extra_money(
    client: httpx.AsyncClient,
    drain_jobs: Drain,
    session: AsyncSession,
    payments: InMemoryPayments,
    clock: MovableClock,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    _, run_id = await paused_priya_refund(
        client, drain_jobs, session, auth_headers, use_agents, wire_app
    )
    _, approval = await approval_for_run(session, run_id)
    reviewer = await auth_headers(REVIEWER)
    assert (await decide(client, drain_jobs, reviewer, approval.id, "approve")).status_code == 202

    # A second decision through the API.
    again = await decide(client, drain_jobs, reviewer, approval.id, "approve")
    assert again.status_code == 409
    # The same resume job delivered twice (at-least-once delivery): the copy finds the run
    # finished and does nothing.
    (first,) = (
        await session.scalars(
            select(Job).where(
                Job.run_id == UUID(run_id), Job.job_type == JobType.RESUME_AFTER_DECISION
            )
        )
    ).all()
    await queue.enqueue(
        session,
        JobType.RESUME_AFTER_DECISION,
        run_id=UUID(run_id),
        payload=dict(first.payload),
        now=clock(),
        max_attempts=1,
    )
    assert await drain_jobs() == 1
    # The customer cannot resume it either.
    priya = await auth_headers("priya@example.com")
    assert (await reply(client, drain_jobs, priya, run_id, "hello?")).status_code == 409

    assert await refund_rows(session, run_id) == 1
    assert await payments.refund_count() == 1
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.COMPLETED
    assert len(await events(session, run_id, "run.resumed")) == 1
    jobs = (await session.scalars(select(Job).where(Job.run_id == UUID(run_id)))).all()
    assert {job.status for job in jobs} == {JobStatus.SUCCEEDED}


# ---------------------------------------------------------------- customer pause


async def test_a_customer_reply_resumes_the_run_and_completes_it(
    client: httpx.AsyncClient,
    drain_jobs: Drain,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
) -> None:
    use_agents(arjun_explain_scripts([classified("order_status", 0.3), classified("order_status")]))
    arjun = await auth_headers("arjun@example.com")
    ticket_id, run_id = await start_run(client, drain_jobs, arjun, "Laptop?", "ORD-1002")
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.AWAITING_CUSTOMER
    assert (await reload(session, Ticket, ticket_id)).status == TicketStatus.AWAITING_CUSTOMER

    customer_words = "REPLY-TEXT where is my laptop, it has not arrived"
    resumed = await reply(client, drain_jobs, arjun, run_id, customer_words)

    assert resumed.status_code == 202
    assert resumed.json()["status"] == "investigating"  # claimed; the worker did the rest
    run = await reload(session, WorkflowRun, run_id)
    ticket = await reload(session, Ticket, ticket_id)
    assert run.status == RunStatus.COMPLETED
    assert ticket.status == TicketStatus.RESOLVED
    assert ticket.customer_reply == "Your laptop is with the carrier."
    (resumed_event,) = await events(session, run_id, "run.resumed")
    assert resumed_event["resumed_by"] == "customer"
    assert resumed_event["reason"] == "awaiting_customer"
    # Each event was recorded once, and the reply text is in none of them.
    assert len(await events(session, run_id, "run.classified")) == 2
    assert len(await events(session, run_id, "investigation.started")) == 1
    assert "REPLY-TEXT" not in await all_payload_text(session, run_id)
    # The job carries the message's id, never its text.
    (reply_job,) = (
        await session.scalars(
            select(Job).where(
                Job.run_id == UUID(run_id), Job.job_type == JobType.RESUME_AFTER_CUSTOMER_REPLY
            )
        )
    ).all()
    assert set(reply_job.payload) == {"message_id", "checkpoint_id"}
    assert "REPLY-TEXT" not in json.dumps(reply_job.payload)


async def test_only_the_owner_can_reply(
    client: httpx.AsyncClient,
    drain_jobs: Drain,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
) -> None:
    use_agents(arjun_explain_scripts([classified("unclear")]))
    arjun = await auth_headers("arjun@example.com")
    _, run_id = await start_run(client, drain_jobs, arjun, "Hmm", "ORD-1002")

    other = await reply(
        client, drain_jobs, await auth_headers("priya@example.com"), run_id, "mine now"
    )
    staff = await reply(client, drain_jobs, await auth_headers(REVIEWER), run_id, "hello")

    assert other.status_code == 404  # looks exactly like a missing run
    assert staff.status_code == 403
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.AWAITING_CUSTOMER


async def test_the_clarification_limit_escalates(
    client: httpx.AsyncClient,
    drain_jobs: Drain,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    rounds = wire_app.max_clarification_rounds
    use_agents({"classifier": [classified("unclear")] * (rounds + 1)})
    arjun = await auth_headers("arjun@example.com")
    ticket_id, run_id = await start_run(client, drain_jobs, arjun, "Hmm", "ORD-1002")

    for n in range(rounds):
        response = await reply(client, drain_jobs, arjun, run_id, f"answer {n}")
        assert response.status_code == 202

    run = await reload(session, WorkflowRun, run_id)
    assert run.status == RunStatus.ESCALATED
    assert (await reload(session, Ticket, ticket_id)).status == TicketStatus.ESCALATED
    assert len(await events(session, run_id, "run.paused")) == rounds
    # Finished: one more reply is refused and changes nothing.
    assert (await reply(client, drain_jobs, arjun, run_id, "one more")).status_code == 409
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.ESCALATED
