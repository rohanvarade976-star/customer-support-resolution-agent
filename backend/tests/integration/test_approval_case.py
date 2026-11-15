"""The reviewer's case view, GET /approvals/{approval_id} (ADR-019).

Checked against database state: the amount is the stored proposal's, citations exist in
policy_chunks and carry our own text, the refund status is the refund row's. Reviewers only.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
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
from support_agent.db.models import (
    ActionProposal,
    Approval,
    Customer,
    PolicyChunk,
    RefundOperation,
)
from support_agent.db.seed import load_scenarios, seed
from support_agent.integrations.payments import PaymentClient
from support_agent.llm.fake import ScriptStep, tool_reply
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.repositories import policies
from support_agent.services import policy_search

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)  # the mock shipping provider's instant
REVIEWER = "anita@example.com"
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
    """The policy citation Priya's run will cite: a real chunk key."""
    priya_id = await session.scalar(
        select(Customer.id).where(Customer.email == "priya@example.com")
    )
    assert priya_id is not None
    found = await policy_search.search_policy(
        session, FakeEmbedder(), wire_app, priya_id, "return window", "ORD-1001", NOW
    )
    return found.passages[0].citation


@pytest.fixture
async def approval_id(
    client: httpx.AsyncClient,
    session: AsyncSession,
    use_agents: UseAgents,
    auth_headers: HeadersFor,
    drain_jobs: Drain,
    cited: str,
) -> UUID:
    """Priya's refund run, paused awaiting approval; the id of its approval."""
    use_agents(
        {
            "classifier": [
                tool_reply(
                    "classification", {"category": "refund", "confidence": 0.95, "reason": "t"}
                )
            ],
            "supervisor": [supervisor_reply()],
            "investigator": simple_investigation("ORD-1001"),
            "policy": policy_run(cited),
            "resolution": [draft_reply("refund", citations=[cited])],
            "critic": [critique_reply("approve")],
        }
    )
    headers = await auth_headers("priya@example.com")
    body = {"message": "I returned my headphones", "order_number": "ORD-1001"}
    created = await client.post("/tickets", json=body, headers=headers)
    started = await client.post(f"/tickets/{created.json()['id']}/runs", headers=headers)
    assert started.status_code == 202
    await drain_jobs()
    stmt = (
        select(Approval.id)
        .join(ActionProposal, ActionProposal.id == Approval.proposal_id)
        .where(ActionProposal.run_id == UUID(str(started.json()["run_id"])))
    )
    found = await session.scalar(stmt)
    assert found is not None
    return found


async def _proposal(session: AsyncSession, approval_id: UUID) -> ActionProposal:
    approval = await session.get(Approval, approval_id, populate_existing=True)
    assert approval is not None
    proposal = await session.get(ActionProposal, approval.proposal_id, populate_existing=True)
    assert proposal is not None
    return proposal


async def test_customers_are_refused(
    client: httpx.AsyncClient, approval_id: UUID, auth_headers: HeadersFor
) -> None:
    response = await client.get(
        f"/approvals/{approval_id}", headers=await auth_headers("priya@example.com")
    )
    assert response.status_code == 403
    assert (await client.get(f"/approvals/{approval_id}")).status_code == 401


async def test_unknown_approval_is_404(client: httpx.AsyncClient, auth_headers: HeadersFor) -> None:
    response = await client.get(f"/approvals/{UUID(int=3)}", headers=await auth_headers(REVIEWER))
    assert response.status_code == 404
    assert response.json()["error_code"] == "APPROVAL_NOT_FOUND"


async def test_pending_case_shows_stored_amount_facts_and_real_citations(
    client: httpx.AsyncClient,
    session: AsyncSession,
    approval_id: UUID,
    auth_headers: HeadersFor,
    cited: str,
) -> None:
    response = await client.get(f"/approvals/{approval_id}", headers=await auth_headers(REVIEWER))
    assert response.status_code == 200
    case = response.json()
    proposal = await _proposal(session, approval_id)

    assert case["approval_status"] == "pending"
    assert case["proposal"]["amount_minor"] == proposal.payload["amount_minor"] == 249900
    assert case["proposal"]["payload_hash"] == proposal.payload_hash
    assert case["facts"]["eligible_now"] is True
    assert case["facts"]["refundable_balance_minor"] == 249900
    assert case["governing_policy"]["policy_key"] == "refund_policy"
    assert case["refund"] is None

    assert [c["citation"] for c in case["citations"]] == [cited]
    chunk = await session.scalar(select(PolicyChunk).where(PolicyChunk.chunk_key == cited))
    assert chunk is not None
    assert case["citations"][0]["text"] == chunk.content  # our policy text, not the model's


async def test_after_approval_the_case_shows_the_refund_row(
    client: httpx.AsyncClient,
    session: AsyncSession,
    approval_id: UUID,
    auth_headers: HeadersFor,
    drain_jobs: Drain,
) -> None:
    headers = await auth_headers(REVIEWER)
    body = {"decision": "approve", "note": "checked"}
    decided = await client.post(f"/approvals/{approval_id}/decision", json=body, headers=headers)
    assert decided.status_code == 202
    await drain_jobs()

    case = (await client.get(f"/approvals/{approval_id}", headers=headers)).json()
    proposal = await _proposal(session, approval_id)
    operation = await session.scalar(
        select(RefundOperation).where(RefundOperation.proposal_id == proposal.id)
    )
    assert operation is not None
    assert case["approval_status"] == "approved"
    assert case["refund"]["refund_id"] == str(operation.id)
    assert case["refund"]["status"] == operation.status.value
    assert case["refund"]["amount_minor"] == operation.amount_minor == 249900


async def test_unknown_citation_keys_are_dropped(session: AsyncSession, cited: str) -> None:
    """A model-chosen key that is not in policy_chunks is never shown."""
    rows = await policies.get_chunks_by_keys(session, ["made_up_policy_v9:section_1", cited])
    assert [chunk.chunk_key for chunk, _document in rows] == [cited]
