"""Refund proposals, human approval and refund execution, end to end through the API.

The app's payment client talks to the real mock payments app in memory (ASGITransport).
Money checks count rows in our refund_operations table and in mock_payments.refunds,
never response text.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from support_agent.api.deps import get_clock, get_payment_client
from support_agent.core.config import Settings, get_settings
from support_agent.db.models import (
    ActionProposal,
    Approval,
    AuditEvent,
    Order,
    RefundOperation,
    StaffUser,
    Ticket,
    WorkflowRun,
)
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import (
    ApprovalDecision,
    ProposalStatus,
    RefundOperationStatus,
    RunMode,
    RunStatus,
    TicketStatus,
)
from support_agent.integrations.payments import PaymentClient
from support_agent.services.refund_execution import execute_approved_refund

Headers = dict[str, str]
AuthHeaders = Callable[[str], Awaitable[Headers]]

REVIEWER = "anita@example.com"
PRIYA_AMOUNT = 249900  # ORD-1001, paise


# ---------------------------------------------------------------- fixtures


class MutableClock:
    """The app's clock in these tests. Tests move it forward to make approvals expire."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class RefusingTransport(httpx.AsyncBaseTransport):
    """Behaves like a payment provider we cannot even connect to."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)


@dataclass
class Payments:
    app: FastAPI
    engine: AsyncEngine

    def client(self, transport: httpx.AsyncBaseTransport | None = None) -> PaymentClient:
        return PaymentClient(_client_settings(), transport or httpx.ASGITransport(app=self.app))

    async def refund_count(self) -> int:
        async with self.engine.connect() as conn:
            return int(await conn.scalar(text("SELECT count(*) FROM mock_payments.refunds")) or 0)

    async def set_fault(self, **config: Any) -> None:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://admin") as admin:
            response = await admin.post("/_admin/faults", json=config)
        assert response.status_code == 200


def _client_settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+asyncpg://user:pw@localhost:5432/unused"),
        jwt_secret=SecretStr("x" * 32),
        payments_base_url="http://payments.mock",
    )


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture(autouse=True)
async def seeded(
    session: AsyncSession, demo_password_hash: str, clock: MutableClock, ingested_policies: None
) -> None:
    await seed(session, load_scenarios(), clock.now, demo_password_hash)


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


def use_payment_transport(
    app: FastAPI, payments: Payments, transport: httpx.AsyncBaseTransport
) -> None:
    """Make the app's payment client send its requests through `transport`."""

    async def client() -> AsyncIterator[PaymentClient]:
        async with payments.client(transport) as payment_client:
            yield payment_client

    app.dependency_overrides[get_payment_client] = client


@pytest.fixture(autouse=True)
def wire_app(app: FastAPI, payments: Payments, clock: MutableClock) -> None:
    """Point the app at the mock payments app and the test clock."""
    app.dependency_overrides[get_clock] = lambda: clock
    use_payment_transport(app, payments, httpx.ASGITransport(app=payments.app))


# ---------------------------------------------------------------- helpers


async def open_ticket(
    client: httpx.AsyncClient, auth_headers: AuthHeaders, email: str, order_number: str | None
) -> str:
    body: dict[str, Any] = {"message": "I would like a refund"}
    if order_number is not None:
        body["order_number"] = order_number
    response = await client.post("/tickets", json=body, headers=await auth_headers(email))
    assert response.status_code == 201
    return str(response.json()["id"])


async def propose(client: httpx.AsyncClient, headers: Headers, ticket_id: str) -> httpx.Response:
    return await client.post(f"/tickets/{ticket_id}/refund-proposals", headers=headers)


async def decide(
    client: httpx.AsyncClient,
    headers: Headers,
    approval_id: str,
    decision: str,
    note: str | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"decision": decision}
    if note is not None:
        body["note"] = note
    return await client.post(f"/approvals/{approval_id}/decision", json=body, headers=headers)


async def priya_proposal(client: httpx.AsyncClient, auth_headers: AuthHeaders) -> dict[str, Any]:
    ticket_id = await open_ticket(client, auth_headers, "priya@example.com", "ORD-1001")
    response = await propose(client, await auth_headers(REVIEWER), ticket_id)
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    return body


async def refunds_for(session: AsyncSession, proposal_id: str) -> list[RefundOperation]:
    stmt = select(RefundOperation).where(RefundOperation.proposal_id == UUID(proposal_id))
    return list((await session.scalars(stmt.execution_options(populate_existing=True))).all())


async def all_refunds(session: AsyncSession) -> int:
    return len((await session.scalars(select(RefundOperation.id))).all())


async def event_types(session: AsyncSession, run_id: str) -> list[str]:
    stmt = select(AuditEvent.event_type).where(AuditEvent.run_id == UUID(run_id))
    return list((await session.scalars(stmt)).all())


async def reload[M](session: AsyncSession, model: type[M], row_id: str) -> M:
    row = await session.get(model, UUID(row_id), populate_existing=True)
    assert row is not None
    return row


def assert_error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    assert response.status_code == status, body
    assert body["error_code"] == code
    return body


# ---------------------------------------------------------------- Priya: the happy path


async def test_priya_refund_flows_end_to_end(
    client: httpx.AsyncClient, session: AsyncSession, payments: Payments, auth_headers: AuthHeaders
) -> None:
    reviewer = await auth_headers(REVIEWER)
    proposal = await priya_proposal(client, auth_headers)
    assert proposal["amount_minor"] == PRIYA_AMOUNT
    assert proposal["status"] == "pending_approval"

    queue = (await client.get("/approvals", headers=reviewer)).json()["items"]
    assert len(queue) == 1
    item = queue[0]
    assert item["approval_id"] == proposal["approval_id"]
    assert item["order_number"] == "ORD-1001"
    assert item["customer_name"] == "Priya Sharma"
    assert item["amount_minor"] == PRIYA_AMOUNT
    assert "INSPECTION_PASSED" in item["reasons"]

    response = await decide(client, reviewer, proposal["approval_id"], "approve", "Looks good")
    assert response.status_code == 200
    assert response.json()["refund"]["status"] == "submitted"

    # Exactly one refund, in our table and at the provider.
    rows = await refunds_for(session, proposal["proposal_id"])
    assert len(rows) == 1
    assert rows[0].amount_minor == PRIYA_AMOUNT
    assert rows[0].status == RefundOperationStatus.SUBMITTED
    assert rows[0].idempotency_key == f"refund-{proposal['proposal_id']}"
    assert rows[0].provider_reference is not None
    assert await payments.refund_count() == 1

    # Who decided comes from the token.
    anita_id = await session.scalar(select(StaffUser.id).where(StaffUser.email == REVIEWER))
    approval = await reload(session, Approval, proposal["approval_id"])
    assert approval.decision == ApprovalDecision.APPROVED
    assert approval.reviewer_id == str(anita_id)
    assert approval.decided_at is not None

    assert (await reload(session, ActionProposal, proposal["proposal_id"])).status == (
        ProposalStatus.EXECUTED
    )
    run = await reload(session, WorkflowRun, proposal["run_id"])
    assert run.mode == RunMode.MANUAL
    assert run.status == RunStatus.COMPLETED
    # Submitted is not succeeded, so the ticket is not resolved yet.
    ticket = await reload(session, Ticket, proposal["ticket_id"])
    assert ticket.status == TicketStatus.IN_PROGRESS

    assert (await client.get("/approvals", headers=reviewer)).json()["items"] == []

    events = await event_types(session, proposal["run_id"])
    for expected in (
        "proposal.created",
        "approval.requested",
        "ticket.updated",
        "approval.approved",
        "refund.reserved",
        "refund.submitted",
        "run.completed",
    ):
        assert expected in events


async def test_amount_in_the_request_is_ignored(
    client: httpx.AsyncClient, auth_headers: AuthHeaders
) -> None:
    ticket_id = await open_ticket(client, auth_headers, "priya@example.com", "ORD-1001")
    response = await client.post(
        f"/tickets/{ticket_id}/refund-proposals",
        json={"amount_minor": 1},
        headers=await auth_headers(REVIEWER),
    )
    assert response.status_code == 201
    assert response.json()["amount_minor"] == PRIYA_AMOUNT


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("email", "order_number", "reasons"),
    [
        ("rahul@example.com", "ORD-1004", ["NO_RETURN", "OUTSIDE_RETURN_WINDOW"]),
        ("vikram@example.com", "ORD-1006", ["INSPECTION_FAILED"]),
        ("kavya@example.com", "ORD-1005", ["ALREADY_REFUNDED"]),
    ],
)
async def test_ineligible_orders_get_no_proposal(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: AuthHeaders,
    email: str,
    order_number: str,
    reasons: list[str],
) -> None:
    ticket_id = await open_ticket(client, auth_headers, email, order_number)

    response = await propose(client, await auth_headers(REVIEWER), ticket_id)

    body = assert_error(response, 409, "NOT_ELIGIBLE")
    assert body["details"]["reasons"] == reasons
    runs = await session.scalars(
        select(WorkflowRun).where(WorkflowRun.ticket_id == UUID(ticket_id))
    )
    assert runs.all() == []
    refused = await session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "proposal.refused")
    )
    assert [event.redacted_payload["ticket_id"] for event in refused] == [ticket_id]


async def test_order_without_an_applicable_policy_gets_no_proposal(
    client: httpx.AsyncClient, session: AsyncSession, auth_headers: AuthHeaders
) -> None:
    # Priya's ORD-1001 is eligible today. Move its placed_at before refund policy v1
    # (2025-01-01): no refund policy governs it, so no proposal and no refund may exist.
    order = await session.scalar(select(Order).where(Order.order_number == "ORD-1001"))
    assert order is not None
    order.placed_at = datetime(2024, 12, 1, 10, 0, tzinfo=UTC)
    await session.flush()
    ticket_id = await open_ticket(client, auth_headers, "priya@example.com", "ORD-1001")
    # The seed already holds Kavya's past refund (with its proposal): count, don't assume 0.
    proposals_before = len((await session.scalars(select(ActionProposal.id))).all())
    refunds_before = await all_refunds(session)

    response = await propose(client, await auth_headers(REVIEWER), ticket_id)

    body = assert_error(response, 409, "NOT_ELIGIBLE")
    assert body["details"]["reasons"] == ["NO_APPLICABLE_POLICY"]
    assert len((await session.scalars(select(ActionProposal.id))).all()) == proposals_before
    assert await all_refunds(session) == refunds_before
    refused = await session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "proposal.refused")
    )
    assert [event.redacted_payload["reasons"] for event in refused] == [["NO_APPLICABLE_POLICY"]]


async def test_ticket_without_an_order_cannot_be_proposed(
    client: httpx.AsyncClient, auth_headers: AuthHeaders
) -> None:
    ticket_id = await open_ticket(client, auth_headers, "priya@example.com", None)
    response = await propose(client, await auth_headers(REVIEWER), ticket_id)
    assert_error(response, 409, "TICKET_HAS_NO_ORDER")


async def test_second_proposal_while_one_is_pending_is_refused(
    client: httpx.AsyncClient, auth_headers: AuthHeaders
) -> None:
    proposal = await priya_proposal(client, auth_headers)
    response = await propose(client, await auth_headers(REVIEWER), proposal["ticket_id"])
    assert_error(response, 409, "PROPOSAL_ALREADY_PENDING")


async def test_customers_cannot_propose_list_or_decide(
    client: httpx.AsyncClient, session: AsyncSession, auth_headers: AuthHeaders
) -> None:
    proposal = await priya_proposal(client, auth_headers)
    priya = await auth_headers("priya@example.com")

    assert_error(await propose(client, priya, proposal["ticket_id"]), 403, "FORBIDDEN")
    assert_error(await client.get("/approvals", headers=priya), 403, "FORBIDDEN")
    response = await decide(client, priya, proposal["approval_id"], "approve")
    assert_error(response, 403, "FORBIDDEN")

    approval = await reload(session, Approval, proposal["approval_id"])
    assert approval.decision == ApprovalDecision.PENDING
    assert await all_refunds(session) == 1  # only Kavya's seeded, already-completed refund


async def test_expired_approval_cannot_be_approved(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    clock: MutableClock,
    auth_headers: AuthHeaders,
) -> None:
    reviewer = await auth_headers(REVIEWER)
    proposal = await priya_proposal(client, auth_headers)
    clock.now += timedelta(minutes=get_settings().approval_ttl_minutes, seconds=1)

    response = await decide(client, reviewer, proposal["approval_id"], "approve")

    assert_error(response, 409, "APPROVAL_EXPIRED")
    assert await refunds_for(session, proposal["proposal_id"]) == []
    assert await payments.refund_count() == 0
    approval = await reload(session, Approval, proposal["approval_id"])
    assert approval.decision == ApprovalDecision.EXPIRED
    assert (await reload(session, Ticket, proposal["ticket_id"])).status == TicketStatus.OPEN
    assert "approval.expired" in await event_types(session, proposal["run_id"])
    # A fresh proposal is allowed now.
    assert (await propose(client, reviewer, proposal["ticket_id"])).status_code == 201


async def test_tampered_proposal_is_refused(
    client: httpx.AsyncClient, session: AsyncSession, payments: Payments, auth_headers: AuthHeaders
) -> None:
    proposal = await priya_proposal(client, auth_headers)
    # Someone edits the stored payload behind the API's back.
    await session.execute(
        text(
            "UPDATE action_proposals"
            " SET payload = jsonb_set(payload, '{amount_minor}', '999900') WHERE id = :id"
        ),
        {"id": proposal["proposal_id"]},
    )

    response = await decide(
        client, await auth_headers(REVIEWER), proposal["approval_id"], "approve"
    )

    assert_error(response, 409, "PROPOSAL_CHANGED")
    assert await refunds_for(session, proposal["proposal_id"]) == []
    assert await payments.refund_count() == 0
    stored = await reload(session, ActionProposal, proposal["proposal_id"])
    assert stored.status == ProposalStatus.INVALIDATED
    assert "validation.failed" in await event_types(session, proposal["run_id"])


# ---------------------------------------------------------------- exactly once


async def test_approving_twice_still_yields_one_refund(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    clock: MutableClock,
    auth_headers: AuthHeaders,
) -> None:
    reviewer = await auth_headers(REVIEWER)
    proposal = await priya_proposal(client, auth_headers)
    assert (await decide(client, reviewer, proposal["approval_id"], "approve")).status_code == 200

    second = await decide(client, reviewer, proposal["approval_id"], "approve")
    assert_error(second, 409, "ALREADY_DECIDED")

    # Even calling the executor again directly does not create or send a second refund.
    async with payments.client() as payment_client:
        again = await execute_approved_refund(
            session,
            payment_client,
            UUID(proposal["proposal_id"]),
            now=clock.now,
            settings=get_settings(),
        )
    rows = await refunds_for(session, proposal["proposal_id"])
    assert [row.id for row in rows] == [again.id]
    assert await payments.refund_count() == 1


async def test_rejecting_moves_no_money_and_reopens_the_ticket(
    client: httpx.AsyncClient, session: AsyncSession, payments: Payments, auth_headers: AuthHeaders
) -> None:
    proposal = await priya_proposal(client, auth_headers)

    response = await decide(
        client, await auth_headers(REVIEWER), proposal["approval_id"], "reject", "Check photos"
    )

    assert response.status_code == 200
    assert response.json()["refund"] is None
    assert await refunds_for(session, proposal["proposal_id"]) == []
    assert await payments.refund_count() == 0
    assert (await reload(session, Approval, proposal["approval_id"])).decision == (
        ApprovalDecision.REJECTED
    )
    assert (await reload(session, WorkflowRun, proposal["run_id"])).status == RunStatus.COMPLETED
    assert (await reload(session, Ticket, proposal["ticket_id"])).status == TicketStatus.OPEN
    rejected = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.run_id == UUID(proposal["run_id"]),
            AuditEvent.event_type == "approval.rejected",
        )
    )
    assert rejected is not None
    assert rejected.redacted_payload["note"] == "Check photos"


# ---------------------------------------------------------------- provider failures


async def test_dropped_response_marks_refund_for_reconciliation(
    client: httpx.AsyncClient, session: AsyncSession, payments: Payments, auth_headers: AuthHeaders
) -> None:
    proposal = await priya_proposal(client, auth_headers)
    await payments.set_fault(mode="drop_response", count=1)

    response = await decide(
        client, await auth_headers(REVIEWER), proposal["approval_id"], "approve"
    )

    assert response.status_code == 200
    assert response.json()["refund"]["status"] == "pending_reconciliation"
    [row] = await refunds_for(session, proposal["proposal_id"])
    assert row.status == RefundOperationStatus.PENDING_RECONCILIATION
    assert row.provider_reference is None
    assert (await reload(session, WorkflowRun, proposal["run_id"])).status == RunStatus.EXECUTING
    assert (await reload(session, Ticket, proposal["ticket_id"])).status == (
        TicketStatus.IN_PROGRESS
    )
    assert "refund.reconciling" in await event_types(session, proposal["run_id"])

    # The provider did create it; reconciliation by key finds it.
    async with payments.client() as payment_client:
        found = await payment_client.find_refund_by_key(row.idempotency_key)
    assert found.amount_minor == PRIYA_AMOUNT
    assert await payments.refund_count() == 1


async def test_unreachable_provider_fails_the_refund_without_sending(
    app: FastAPI,
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: Payments,
    auth_headers: AuthHeaders,
) -> None:
    proposal = await priya_proposal(client, auth_headers)
    use_payment_transport(app, payments, RefusingTransport())

    response = await decide(
        client, await auth_headers(REVIEWER), proposal["approval_id"], "approve"
    )

    assert response.status_code == 200
    [row] = await refunds_for(session, proposal["proposal_id"])
    assert row.status == RefundOperationStatus.FAILED
    assert await payments.refund_count() == 0
    assert (await reload(session, WorkflowRun, proposal["run_id"])).status == RunStatus.FAILED
    assert (await reload(session, Ticket, proposal["ticket_id"])).status == TicketStatus.ESCALATED
