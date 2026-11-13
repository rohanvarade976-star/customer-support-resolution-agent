"""POST /tickets/{ticket_id}/runs and GET /runs/{run_id}, against a real database.

The LLM is the scripted fake (never a real API). Money and security outcomes are
checked in the database, not only in the response.

The API only queues the run (202); `drain_jobs()` then plays the background worker.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import AuditEvent, Job, Ticket, WorkflowRun
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import (
    JobStatus,
    JobType,
    RunMode,
    RunStatus,
    TicketCategory,
    TicketStatus,
)
from support_agent.llm.fake import FakeProvider, ScriptStep, tool_reply, transient

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseLLM = Callable[[list[ScriptStep]], FakeProvider]
Drain = Callable[[], Awaitable[int]]


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


def answer(category: str, confidence: float, order_number: str | None = None) -> ScriptStep:
    args: dict[str, Any] = {"category": category, "confidence": confidence, "reason": "test"}
    if order_number is not None:
        args["order_number"] = order_number
    return tool_reply("classification", args)


async def create_ticket(
    client: httpx.AsyncClient, headers: dict[str, str], message: str, order: str | None = None
) -> str:
    body: dict[str, str] = {"message": message}
    if order is not None:
        body["order_number"] = order
    response = await client.post("/tickets", json=body, headers=headers)
    assert response.status_code == 201
    return str(response.json()["id"])


async def events(session: AsyncSession, run_id: str) -> dict[str, dict[str, Any]]:
    """event_type -> payload for one run (each type appears at most once here)."""
    rows = await session.scalars(select(AuditEvent).where(AuditEvent.run_id == UUID(run_id)))
    return {row.event_type: row.redacted_payload for row in rows}


async def run_count(session: AsyncSession, ticket_id: str) -> int:
    """Runs on one ticket (the seed data has runs of its own)."""
    stmt = select(func.count()).where(WorkflowRun.ticket_id == UUID(ticket_id))
    return await session.scalar(stmt) or 0


# ---------------------------------------------------------------- each outcome


async def test_starting_a_run_only_queues_it(
    client: httpx.AsyncClient, session: AsyncSession, auth_headers: HeadersFor, use_llm: UseLLM
) -> None:
    fake = use_llm([answer("refund", 0.95)])
    priya = await auth_headers("priya@example.com")
    ticket_id = await create_ticket(client, priya, "Where is my refund?", order="ORD-1001")

    response = await client.post(f"/tickets/{ticket_id}/runs", headers=priya)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "created"
    assert body["job"] == {
        "job_type": "start_run",
        "status": "queued",
        "attempts": 0,
        "next_attempt_at": body["job"]["next_attempt_at"],
    }
    assert fake.requests == []  # the graph did not run inside the request
    run = await session.get(WorkflowRun, UUID(body["run_id"]))
    assert run is not None and run.status == RunStatus.CREATED
    (job,) = (await session.scalars(select(Job).where(Job.run_id == run.id))).all()
    assert (job.job_type, job.status, job.payload) == (JobType.START_RUN, JobStatus.QUEUED, {})


async def test_confident_run_is_classified_and_saved(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_llm: UseLLM,
    drain_jobs: Drain,
) -> None:
    # A confident label, then the supervisor fails: the label is kept, the run escalates.
    # (The full multi-agent paths are in test_workflow_outcomes.py.)
    use_llm([answer("refund", 0.95), transient()])
    priya = await auth_headers("priya@example.com")
    ticket_id = await create_ticket(client, priya, "Where is my refund?", order="ORD-1001")

    response = await client.post(f"/tickets/{ticket_id}/runs", headers=priya)
    assert response.status_code == 202
    assert await drain_jobs() == 1

    body = (await client.get(f"/runs/{response.json()['run_id']}", headers=priya)).json()
    assert (body["status"], body["category"]) == ("escalated", "refund")
    assert (body["job"]["status"], body["job"]["attempts"]) == ("succeeded", 1)

    run = await session.get(WorkflowRun, UUID(body["run_id"]))
    ticket = await session.get(Ticket, UUID(ticket_id))
    assert run is not None and ticket is not None
    assert (run.status, run.mode) == (RunStatus.ESCALATED, RunMode.MULTI_AGENT)
    assert run.completed_at is not None
    assert run.thread_id == f"run-{run.id}"
    assert (ticket.category, ticket.status) == (TicketCategory.REFUND, TicketStatus.ESCALATED)

    recorded = await events(session, body["run_id"])
    assert set(recorded) == {
        "run.created",
        "run.classified",
        "run.escalated",
        "ticket.updated",
        "run.usage",
    }
    assert recorded["run.escalated"]["error_codes"] == ["LLM_UNAVAILABLE"]
    assert recorded["run.classified"] == {
        "category": "refund",
        "confidence": 0.95,
        "order_hint": "none",  # the model extracted no order number
    }


async def test_low_confidence_run_waits_for_the_customer(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_llm: UseLLM,
    drain_jobs: Drain,
) -> None:
    use_llm([answer("refund", 0.4)])
    priya = await auth_headers("priya@example.com")
    ticket_id = await create_ticket(client, priya, "something is wrong")

    body = (await client.post(f"/tickets/{ticket_id}/runs", headers=priya)).json()
    await drain_jobs()

    run = await session.get(WorkflowRun, UUID(body["run_id"]))
    ticket = await session.get(Ticket, UUID(ticket_id))
    assert run is not None and ticket is not None
    assert run.status == RunStatus.AWAITING_CUSTOMER
    # A guess is not saved as the ticket's category.
    assert (ticket.category, ticket.status) == (None, TicketStatus.AWAITING_CUSTOMER)
    recorded = await events(session, body["run_id"])
    assert recorded["clarification.requested"] == {"reason": "low_confidence"}
    assert recorded["run.classified"]["category"] == "refund"


async def test_llm_failure_escalates_the_run(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_llm: UseLLM,
    drain_jobs: Drain,
) -> None:
    use_llm([transient()])
    priya = await auth_headers("priya@example.com")
    ticket_id = await create_ticket(client, priya, "Where is my refund?", order="ORD-1001")

    response = await client.post(f"/tickets/{ticket_id}/runs", headers=priya)

    assert response.status_code == 202
    await drain_jobs()
    run = await session.get(WorkflowRun, UUID(response.json()["run_id"]))
    ticket = await session.get(Ticket, UUID(ticket_id))
    assert run is not None and ticket is not None
    assert run.status == RunStatus.ESCALATED
    assert run.completed_at is not None
    assert (ticket.category, ticket.status) == (None, TicketStatus.ESCALATED)
    recorded = await events(session, str(run.id))
    assert "run.classified" not in recorded
    assert recorded["run.escalated"]["error_codes"] == ["LLM_UNAVAILABLE"]


async def test_another_customers_order_number_is_ignored(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_llm: UseLLM,
    drain_jobs: Drain,
) -> None:
    # Rahul names Priya's order in the text; the classifier extracts it.
    use_llm([answer("refund", 0.9, order_number="ORD-1001")])
    rahul = await auth_headers("rahul@example.com")
    ticket_id = await create_ticket(client, rahul, "Refund my order ORD-1001 now")

    body = (await client.post(f"/tickets/{ticket_id}/runs", headers=rahul)).json()
    await drain_jobs()

    ticket = await session.get(Ticket, UUID(ticket_id))
    assert ticket is not None and ticket.order_id is None  # never attached to Priya's order
    recorded = await events(session, body["run_id"])
    assert recorded["run.classified"]["order_hint"] == "ignored_not_owned"
    # The rejected order number is not written anywhere in the audit trail.
    assert "ORD-1001" not in str(recorded)


# ---------------------------------------------------------------- who may start a run


async def test_another_customer_cannot_start_a_run_on_my_ticket(
    client: httpx.AsyncClient, session: AsyncSession, auth_headers: HeadersFor, use_llm: UseLLM
) -> None:
    fake = use_llm([])
    ticket_id = await create_ticket(client, await auth_headers("priya@example.com"), "help")

    response = await client.post(
        f"/tickets/{ticket_id}/runs", headers=await auth_headers("rahul@example.com")
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TICKET_NOT_FOUND"
    assert await run_count(session, ticket_id) == 0
    assert fake.requests == []  # the LLM was never called


async def test_reviewer_cannot_start_a_run(
    client: httpx.AsyncClient, session: AsyncSession, auth_headers: HeadersFor, use_llm: UseLLM
) -> None:
    use_llm([])
    ticket_id = await create_ticket(client, await auth_headers("priya@example.com"), "help")

    response = await client.post(
        f"/tickets/{ticket_id}/runs", headers=await auth_headers("anita@example.com")
    )

    assert response.status_code == 403
    assert await run_count(session, ticket_id) == 0


async def test_start_run_without_a_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.post("/tickets/00000000-0000-0000-0000-000000000000/runs")
    assert response.status_code == 401


async def test_second_run_while_one_is_unfinished_is_409(
    client: httpx.AsyncClient, session: AsyncSession, auth_headers: HeadersFor, use_llm: UseLLM
) -> None:
    use_llm([answer("refund", 0.4)])  # a run waiting for the customer is unfinished
    priya = await auth_headers("priya@example.com")
    ticket_id = await create_ticket(client, priya, "Where is my refund?", order="ORD-1001")
    await client.post(f"/tickets/{ticket_id}/runs", headers=priya)

    response = await client.post(f"/tickets/{ticket_id}/runs", headers=priya)

    assert response.status_code == 409
    assert response.json()["error_code"] == "RUN_ALREADY_ACTIVE"
    assert await run_count(session, ticket_id) == 1


# ---------------------------------------------------------------- who may read a run


async def test_owner_and_reviewer_can_read_a_run_others_get_404(
    client: httpx.AsyncClient, auth_headers: HeadersFor, use_llm: UseLLM, drain_jobs: Drain
) -> None:
    use_llm([answer("damaged_item", 0.9), transient()])
    priya = await auth_headers("priya@example.com")
    ticket_id = await create_ticket(client, priya, "It arrived broken", order="ORD-1001")
    run_id = (await client.post(f"/tickets/{ticket_id}/runs", headers=priya)).json()["run_id"]
    await drain_jobs()

    for email in ("priya@example.com", "anita@example.com"):
        response = await client.get(f"/runs/{run_id}", headers=await auth_headers(email))
        assert response.status_code == 200
        assert response.json()["category"] == "damaged_item"

    response = await client.get(f"/runs/{run_id}", headers=await auth_headers("rahul@example.com"))
    assert response.status_code == 404
    assert response.json()["error_code"] == "RUN_NOT_FOUND"

    missing = await client.get("/runs/00000000-0000-0000-0000-000000000000", headers=priya)
    assert missing.status_code == 404
