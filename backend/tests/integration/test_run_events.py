"""A run's live status (FR-22, ADR-019): GET /runs/{id}/events and GET /runs/{id}/stream.

Checked here:
- who may see it: no token 401, another customer 404, the owner and reviewers 200;
- what it carries: customers get customer-visible event types only and no payload, reviewers
  get allowlisted codes; customer text, model text other than the verified reply, and the
  reviewer's note never appear (unique marker strings are searched for in every response);
- the stream: snapshot first, a change is sent when it happens, it ends when the run is
  finished or when its time is up;
- "My tickets": each ticket carries its own latest run, and only the owner's tickets.

The LLM is the scripted AgentFake; everything else is real (checkpointer, worker handlers).
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
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
from support_agent.api.deps import get_clock, get_payment_client
from support_agent.core.config import Settings, get_settings
from support_agent.db.models import ActionProposal, Approval, Customer, Ticket, WorkflowRun
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import Role, RunStatus
from support_agent.domain.run_events import CUSTOMER_EVENT_TYPES, REVIEWER_PAYLOAD_KEYS
from support_agent.integrations.payments import PaymentClient
from support_agent.llm.fake import ScriptStep, tool_reply
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.schemas.auth import Principal
from support_agent.services import policy_search, run_events
from support_agent.workflow.deps import BranchSessions

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)  # the mock shipping provider's instant
REVIEWER = "anita@example.com"
# Unique strings that must never leave the API through these endpoints.
COMPLAINT_MARKER = "ZZCOMPLAINTMARKER41"
NOTE_MARKER = "ZZNOTEMARKER77"
HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseAgents = Callable[[dict[str, list[ScriptStep]]], AgentFake]
Drain = Callable[[], Awaitable[int]]


@pytest.fixture(autouse=True)
def wire_app(app: FastAPI, payments: InMemoryPayments) -> Settings:
    """A fixed clock, the in-memory payment provider, a policy search the fake embedder
    passes, and a fast stream (short poll, short life) so tests finish quickly."""
    app.dependency_overrides[get_clock] = lambda: lambda: NOW

    async def payment_client() -> AsyncIterator[PaymentClient]:
        async with payments.client() as client:
            yield client

    app.dependency_overrides[get_payment_client] = payment_client
    settings = get_settings().model_copy(
        update={
            "policy_min_similarity": -1.0,
            "sse_poll_interval_seconds": 0.05,
            "sse_max_stream_seconds": 0.4,
            "sse_heartbeat_seconds": 0.1,
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str, ingested_policies: None) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


async def _priya_scripts(session: AsyncSession, settings: Settings) -> dict[str, list[ScriptStep]]:
    priya_id = await session.scalar(
        select(Customer.id).where(Customer.email == "priya@example.com")
    )
    assert priya_id is not None
    found = await policy_search.search_policy(
        session, FakeEmbedder(), settings, priya_id, "return window", "ORD-1001", NOW
    )
    citation = found.passages[0].citation
    return {
        "classifier": [
            tool_reply("classification", {"category": "refund", "confidence": 0.95, "reason": "t"})
        ],
        "supervisor": [supervisor_reply()],
        "investigator": simple_investigation("ORD-1001"),
        "policy": policy_run(citation),
        "resolution": [draft_reply("refund", citations=[citation])],
        "critic": [critique_reply("approve")],
    }


@pytest.fixture
async def paused_run(
    client: httpx.AsyncClient,
    session: AsyncSession,
    use_agents: UseAgents,
    auth_headers: HeadersFor,
    drain_jobs: Drain,
    wire_app: Settings,
) -> str:
    """Priya's refund run, paused awaiting approval. Her complaint carries a marker."""
    use_agents(await _priya_scripts(session, wire_app))
    headers = await auth_headers("priya@example.com")
    body = {"message": f"I returned my headphones {COMPLAINT_MARKER}", "order_number": "ORD-1001"}
    created = await client.post("/tickets", json=body, headers=headers)
    assert created.status_code == 201
    started = await client.post(f"/tickets/{created.json()['id']}/runs", headers=headers)
    assert started.status_code == 202
    await drain_jobs()
    run_id = str(started.json()["run_id"])
    run = await session.get(WorkflowRun, UUID(run_id), populate_existing=True)
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL
    return run_id


async def _approval_id(session: AsyncSession, run_id: str) -> UUID:
    stmt = (
        select(Approval.id)
        .join(ActionProposal, ActionProposal.id == Approval.proposal_id)
        .where(ActionProposal.run_id == UUID(run_id))
    )
    approval_id = await session.scalar(stmt)
    assert approval_id is not None
    return approval_id


async def _reject_with_marked_note(
    client: httpx.AsyncClient,
    session: AsyncSession,
    drain: Drain,
    headers: dict[str, str],
    run_id: str,
) -> None:
    approval_id = await _approval_id(session, run_id)
    body: dict[str, Any] = {"decision": "reject", "note": f"not convinced {NOTE_MARKER}"}
    response = await client.post(f"/approvals/{approval_id}/decision", json=body, headers=headers)
    assert response.status_code == 202
    await drain()


async def _stored_reply(session: AsyncSession, run_id: str) -> str | None:
    run = await session.get(WorkflowRun, UUID(run_id), populate_existing=True)
    assert run is not None
    ticket = await session.get(Ticket, run.ticket_id, populate_existing=True)
    assert ticket is not None
    return ticket.customer_reply


def _sse_events(body: str) -> list[tuple[str, Any]]:
    """(event name, parsed data) for each event in a text/event-stream body."""
    events: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if not lines:
            continue
        fields = dict(line.split(": ", 1) for line in lines)
        events.append((fields["event"], json.loads(fields["data"])))
    return events


# ---------------------------------------------------------------- who may see it


async def test_requires_a_token(client: httpx.AsyncClient, paused_run: str) -> None:
    assert (await client.get(f"/runs/{paused_run}/events")).status_code == 401
    assert (await client.get(f"/runs/{paused_run}/stream")).status_code == 401


async def test_another_customer_gets_404(
    client: httpx.AsyncClient, paused_run: str, auth_headers: HeadersFor
) -> None:
    arjun = await auth_headers("arjun@example.com")
    for path in (f"/runs/{paused_run}/events", f"/runs/{paused_run}/stream"):
        response = await client.get(path, headers=arjun)
        assert response.status_code == 404
        assert response.json()["error_code"] == "RUN_NOT_FOUND"


async def test_unknown_run_is_404(client: httpx.AsyncClient, auth_headers: HeadersFor) -> None:
    headers = await auth_headers(REVIEWER)
    response = await client.get(f"/runs/{UUID(int=7)}/events", headers=headers)
    assert response.status_code == 404


# ---------------------------------------------------------------- what it carries


async def test_customer_snapshot_has_codes_and_the_verified_reply_only(
    client: httpx.AsyncClient, session: AsyncSession, paused_run: str, auth_headers: HeadersFor
) -> None:
    response = await client.get(
        f"/runs/{paused_run}/events", headers=await auth_headers("priya@example.com")
    )
    assert response.status_code == 200
    snapshot = response.json()

    assert snapshot["run"]["status"] == "awaiting_approval"
    assert snapshot["run"]["finished"] is False
    # The reply is exactly what the database holds for the ticket (guarded, verified).
    assert snapshot["run"]["reply"] == await _stored_reply(session, paused_run)
    types = [step["type"] for step in snapshot["steps"]]
    assert "approval.requested" in types
    assert set(types) <= CUSTOMER_EVENT_TYPES  # no tool calls, usage or supervisor steps
    assert all(step["details"] == {} for step in snapshot["steps"])
    assert COMPLAINT_MARKER not in response.text


async def test_reviewer_snapshot_has_allowlisted_details_only(
    client: httpx.AsyncClient, paused_run: str, auth_headers: HeadersFor
) -> None:
    response = await client.get(f"/runs/{paused_run}/events", headers=await auth_headers(REVIEWER))
    assert response.status_code == 200
    steps = response.json()["steps"]

    types = {step["type"] for step in steps}
    assert types - CUSTOMER_EVENT_TYPES, "reviewers also see internal steps (tool calls, ...)"
    for step in steps:
        assert set(step["details"]) <= REVIEWER_PAYLOAD_KEYS
    proposal = next(step for step in steps if step["type"] == "proposal.created")
    assert proposal["details"]["amount_minor"] == 249900
    assert COMPLAINT_MARKER not in response.text


async def test_reviewer_note_never_appears(
    client: httpx.AsyncClient,
    session: AsyncSession,
    paused_run: str,
    auth_headers: HeadersFor,
    drain_jobs: Drain,
) -> None:
    reviewer = await auth_headers(REVIEWER)
    await _reject_with_marked_note(client, session, drain_jobs, reviewer, paused_run)

    for headers in (reviewer, await auth_headers("priya@example.com")):
        for path in (f"/runs/{paused_run}/events", f"/runs/{paused_run}/stream"):
            response = await client.get(path, headers=headers)
            assert response.status_code == 200
            assert NOTE_MARKER not in response.text
            assert COMPLAINT_MARKER not in response.text


# ---------------------------------------------------------------- the stream


async def test_stream_of_a_finished_run_sends_the_snapshot_then_ends(
    client: httpx.AsyncClient,
    session: AsyncSession,
    paused_run: str,
    auth_headers: HeadersFor,
    drain_jobs: Drain,
) -> None:
    await _reject_with_marked_note(
        client, session, drain_jobs, await auth_headers(REVIEWER), paused_run
    )

    response = await client.get(
        f"/runs/{paused_run}/stream", headers=await auth_headers("priya@example.com")
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    events = _sse_events(response.text)

    assert events[0][0] == "status"
    assert events[0][1]["status"] == "completed"
    assert events[0][1]["reply"] == await _stored_reply(session, paused_run)
    assert "approval.rejected" in [data["type"] for name, data in events if name == "step"]
    assert events[-1] == ("end", {"reason": "finished"})


async def test_stream_of_a_paused_run_ends_when_its_time_is_up(
    client: httpx.AsyncClient, paused_run: str, auth_headers: HeadersFor
) -> None:
    response = await client.get(
        f"/runs/{paused_run}/stream", headers=await auth_headers("priya@example.com")
    )
    assert response.status_code == 200
    assert response.text.startswith("event: status")
    assert ": keep-alive" in response.text  # nothing changed, so heartbeats were sent
    assert _sse_events(response.text)[-1] == ("end", {"reason": "timeout"})


async def test_stream_sends_a_change_when_it_happens(
    session: AsyncSession, branch_sessions: BranchSessions, paused_run: str, wire_app: Settings
) -> None:
    """Drives the generator directly, changing the database between two polls."""
    priya = await session.scalar(select(Customer).where(Customer.email == "priya@example.com"))
    assert priya is not None
    principal = Principal(id=priya.id, role=Role.CUSTOMER, name=priya.name, email=priya.email)
    first = await run_events.load_snapshot(session, principal, UUID(paused_run))
    stream = run_events.stream_run(branch_sessions, principal, first, wire_app)

    initial = [await anext(stream) for _ in range(1 + len(first.steps))]
    assert initial[0].startswith("event: status")

    run = await session.get(WorkflowRun, UUID(paused_run))
    assert run is not None
    run.status = RunStatus.EXECUTING
    await session.flush()

    frame = await anext(stream)
    while frame.startswith(":"):  # skip heartbeats
        frame = await anext(stream)
    name, data = _sse_events(frame)[0]
    assert name == "status"
    assert data["status"] == "executing"
    await stream.aclose()


# ---------------------------------------------------------------- "My tickets"


async def test_my_tickets_show_their_latest_run_and_only_mine(
    client: httpx.AsyncClient, paused_run: str, auth_headers: HeadersFor
) -> None:
    priya = await client.get("/tickets", headers=await auth_headers("priya@example.com"))
    assert priya.status_code == 200
    mine = [item for item in priya.json()["items"] if item["latest_run_id"] == paused_run]
    assert len(mine) == 1
    assert mine[0]["latest_run_status"] == "awaiting_approval"

    arjun = await client.get("/tickets", headers=await auth_headers("arjun@example.com"))
    assert arjun.status_code == 200
    assert paused_run not in arjun.text
