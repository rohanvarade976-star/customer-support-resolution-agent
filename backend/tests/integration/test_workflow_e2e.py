"""Priya, end to end through the API: the agents propose, a human approves, one refund executes.

    customer starts a run  ->  the workflow creates a proposal and a pending approval
    reviewer approves via the existing endpoint  ->  the refund executes exactly once

The LLM is the scripted `AgentFake`. Everything else is real: the tools, the eligibility
rules, the policy search, the approval endpoint, the refund executor, and the mock payment
provider (in memory, behind the app's payment client). Money checks count rows in our
refund_operations table and in mock_payments.refunds, never response text.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from mock_providers.payments.app import create_app as create_payments_app
from mock_providers.payments.config import PaymentsSettings
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
    RefundOperation,
    Ticket,
    WorkflowRun,
)
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import (
    ApprovalDecision,
    ProposalStatus,
    RefundOperationStatus,
    RunStatus,
    TicketStatus,
)
from support_agent.integrations.payments import PaymentClient
from support_agent.llm.fake import ScriptStep, tool_reply
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.services import policy_search

# Must equal the instant the mock shipping provider runs at (tests/integration/conftest.py),
# or the order's and the carrier's delivery times would disagree.
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PRIYA_AMOUNT = 249900
REVIEWER = "anita@example.com"
HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseAgents = Callable[[dict[str, list[ScriptStep]]], AgentFake]


@dataclass
class Payments:
    """The in-memory mock payment provider and a way to count the refunds it holds."""

    app: FastAPI
    engine: AsyncEngine

    def client(self) -> PaymentClient:
        settings = Settings(
            _env_file=None,
            database_url=SecretStr("postgresql+asyncpg://user:pw@localhost:5432/unused"),
            jwt_secret=SecretStr("x" * 32),
            payments_base_url="http://payments.mock",
        )
        return PaymentClient(settings, httpx.ASGITransport(app=self.app))

    async def refund_count(self) -> int:
        async with self.engine.connect() as conn:
            return int(await conn.scalar(text("SELECT count(*) FROM mock_payments.refunds")) or 0)


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def payments(test_database_url: str) -> AsyncIterator[Payments]:
    settings = PaymentsSettings(app_env="test", database_url=SecretStr(test_database_url))
    app = create_payments_app(settings)
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    async with app.router.lifespan_context(app):  # ASGITransport does not run lifespan
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE mock_payments.refunds"))
        yield Payments(app, engine)
    await engine.dispose()


async def decide(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    approval_id: str,
    decision: str,
    note: str = "ok",
) -> httpx.Response:
    body: dict[str, Any] = {"decision": decision, "note": note}
    return await client.post(f"/approvals/{approval_id}/decision", json=body, headers=headers)


async def refunds_for(session: AsyncSession, proposal_id: str) -> list[RefundOperation]:
    stmt = select(RefundOperation).where(RefundOperation.proposal_id == UUID(proposal_id))
    return list((await session.scalars(stmt.execution_options(populate_existing=True))).all())


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture(autouse=True)
def wire_app(app: FastAPI, payments: Payments, clock: FixedClock) -> Settings:
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


async def priya_scripts(session: AsyncSession, settings: Settings) -> dict[str, list[ScriptStep]]:
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


async def test_priya_run_then_reviewer_approval_executes_exactly_one_refund(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    use_agents(await priya_scripts(session, wire_app))
    priya = await auth_headers("priya@example.com")
    reviewer = await auth_headers(REVIEWER)
    refunds_before = len((await session.scalars(select(RefundOperation.id))).all())

    # 1. The customer's run: the agents propose, and nothing has moved yet.
    created = await client.post(
        "/tickets",
        json={"message": "Where is my refund?", "order_number": "ORD-1001"},
        headers=priya,
    )
    ticket_id = created.json()["id"]
    started = await client.post(f"/tickets/{ticket_id}/runs", headers=priya)
    assert started.status_code == 202
    run_id = started.json()["run_id"]

    run = await session.get(WorkflowRun, UUID(run_id), populate_existing=True)
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL
    (proposal,) = (
        await session.scalars(select(ActionProposal).where(ActionProposal.run_id == run.id))
    ).all()
    (approval,) = (
        await session.scalars(select(Approval).where(Approval.proposal_id == proposal.id))
    ).all()
    assert proposal.status == ProposalStatus.PENDING_APPROVAL
    assert proposal.payload["amount_minor"] == PRIYA_AMOUNT
    assert approval.decision == ApprovalDecision.PENDING
    assert await payments.refund_count() == 0
    assert len((await session.scalars(select(RefundOperation.id))).all()) == refunds_before

    # 2. The reviewer sees it in the existing queue and approves through the existing endpoint.
    queue = (await client.get("/approvals", headers=reviewer)).json()["items"]
    assert [item["approval_id"] for item in queue] == [str(approval.id)]
    decided = await decide(client, reviewer, str(approval.id), "approve", "Looks right")
    assert decided.status_code == 200

    # 3. Exactly one refund, in our table and at the provider, for the eligible amount.
    rows = await refunds_for(session, str(proposal.id))
    assert len(rows) == 1
    assert rows[0].amount_minor == PRIYA_AMOUNT
    assert rows[0].status == RefundOperationStatus.SUBMITTED
    assert rows[0].idempotency_key == f"refund-{proposal.id}"
    assert await payments.refund_count() == 1
    refreshed = await session.get(ActionProposal, proposal.id, populate_existing=True)
    assert refreshed is not None and refreshed.status == ProposalStatus.EXECUTED

    # Approving a second time cannot create a second refund.
    again = await decide(client, reviewer, str(approval.id), "approve")
    assert again.status_code == 409
    assert await payments.refund_count() == 1
    ticket = await session.get(Ticket, UUID(ticket_id), populate_existing=True)
    assert ticket is not None and ticket.status == TicketStatus.IN_PROGRESS  # not resolved yet


async def test_a_rejected_proposal_from_a_run_moves_no_money(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    use_agents(await priya_scripts(session, wire_app))
    priya = await auth_headers("priya@example.com")
    reviewer = await auth_headers(REVIEWER)
    created = await client.post(
        "/tickets",
        json={"message": "Where is my refund?", "order_number": "ORD-1001"},
        headers=priya,
    )
    started = await client.post(f"/tickets/{created.json()['id']}/runs", headers=priya)
    run = await session.get(WorkflowRun, UUID(started.json()["run_id"]), populate_existing=True)
    assert run is not None
    (proposal,) = (
        await session.scalars(select(ActionProposal).where(ActionProposal.run_id == run.id))
    ).all()
    (approval,) = (
        await session.scalars(select(Approval).where(Approval.proposal_id == proposal.id))
    ).all()

    rejected = await decide(client, reviewer, str(approval.id), "reject", "Not this time")

    assert rejected.status_code == 200
    assert await refunds_for(session, str(proposal.id)) == []
    assert await payments.refund_count() == 0
