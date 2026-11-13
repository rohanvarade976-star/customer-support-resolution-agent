"""The multi-agent workflow through the API with a real database, one test per outcome.

The LLM is the scripted `AgentFake` (never a real API); the tools, services, eligibility
rules and policy search are the real ones. Money and security outcomes are asserted on
database rows (proposals, approvals, refund operations, statuses, audit events), not on
reply text.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mock_providers.shipping.app import create_app as create_shipping_app
from mock_providers.shipping.config import ShippingSettings
from support_agent.agents.fake import (
    AgentFake,
    critique_reply,
    draft_reply,
    plan_reply,
    policy_run,
    read_call,
    search_call,
    simple_investigation,
    summary_reply,
    supervisor_reply,
)
from support_agent.agents.resolve import ESCALATION_MESSAGE
from support_agent.core.config import Settings, get_settings
from support_agent.db.models import (
    ActionProposal,
    Approval,
    AuditEvent,
    Customer,
    RefundOperation,
    Ticket,
    WorkflowRun,
)
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import (
    ApprovalDecision,
    ProposalStatus,
    Role,
    RunMode,
    RunStatus,
    TicketCategory,
    TicketStatus,
)
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.fake import ScriptStep, text_reply, tool_reply, transient
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.schemas.auth import Principal
from support_agent.services import policy_search
from support_agent.services import runs as runs_service
from support_agent.workflow.deps import BranchSessions
from support_agent.workflow.fake import scripted_client

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PRIYA_AMOUNT = 249900
HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseAgents = Callable[[dict[str, list[ScriptStep]]], AgentFake]


@pytest.fixture(autouse=True)
def permissive_policy_search(app: FastAPI) -> Settings:
    """The fake embedder has no meaning, so let every passage count as relevant."""
    settings = get_settings().model_copy(update={"policy_min_similarity": -1.0})
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str, ingested_policies: None) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


def classified(category: str) -> ScriptStep:
    return tool_reply(
        "classification", {"category": category, "confidence": 0.95, "reason": "test"}
    )


async def customer_id(session: AsyncSession, email: str) -> UUID:
    found = await session.scalar(select(Customer.id).where(Customer.email == email))
    assert found is not None
    return found


async def policy_citation(session: AsyncSession, settings: Settings, email: str, order: str) -> str:
    """A real citation the policy search returns for this order (with the fake embedder)."""
    result = await policy_search.search_policy(
        session,
        FakeEmbedder(),
        settings,
        await customer_id(session, email),
        "return window",
        order,
        NOW,
    )
    assert result.passages, "the test policies should answer a 'return window' search"
    return result.passages[0].citation


async def start_run(
    client: httpx.AsyncClient, headers: dict[str, str], message: str, order: str | None
) -> tuple[str, str]:
    """Create a ticket and start its run. Returns (ticket_id, run_id)."""
    body: dict[str, str] = {"message": message}
    if order is not None:
        body["order_number"] = order
    created = await client.post("/tickets", json=body, headers=headers)
    assert created.status_code == 201
    ticket_id = str(created.json()["id"])
    response = await client.post(f"/tickets/{ticket_id}/runs", headers=headers)
    assert response.status_code == 202
    return ticket_id, str(response.json()["run_id"])


async def reload[M](session: AsyncSession, model: type[M], row_id: str) -> M:
    row = await session.get(model, UUID(row_id), populate_existing=True)
    assert row is not None
    return row


async def audit_rows(session: AsyncSession, run_id: str) -> list[AuditEvent]:
    return list(await session.scalars(select(AuditEvent).where(AuditEvent.run_id == UUID(run_id))))


def payloads(rows: list[AuditEvent], event_type: str) -> list[dict[str, Any]]:
    return [row.redacted_payload for row in rows if row.event_type == event_type]


async def count(session: AsyncSession, model: Any, *where: Any) -> int:
    stmt = select(func.count()).select_from(model).where(*where)
    return int(await session.scalar(stmt) or 0)


async def priya_scripts(
    session: AsyncSession, settings: Settings, **draft_extra: Any
) -> dict[str, list[ScriptStep]]:
    citation = await policy_citation(session, settings, "priya@example.com", "ORD-1001")
    return {
        "classifier": [classified("refund")],
        "supervisor": [supervisor_reply()],
        "investigator": simple_investigation("ORD-1001"),
        "policy": policy_run(citation),
        "resolution": [draft_reply("refund", citations=[citation], **draft_extra)],
        "critic": [critique_reply("approve")],
    }


# ---------------------------------------------------------------- refund: proposal and approval


async def test_priya_run_creates_a_pending_approval_and_moves_no_money(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    permissive_policy_search: Settings,
) -> None:
    # The model tries to name its own amount: it is ignored.
    scripts = await priya_scripts(session, permissive_policy_search, amount_minor=1)
    use_agents(scripts)
    priya = await auth_headers("priya@example.com")

    refunds_before = await count(session, RefundOperation)

    ticket_id, run_id = await start_run(client, priya, "COMPLAINT-TEXT refund please", "ORD-1001")

    run = await reload(session, WorkflowRun, run_id)
    ticket = await reload(session, Ticket, ticket_id)
    assert (run.status, ticket.status) == (RunStatus.AWAITING_APPROVAL, TicketStatus.IN_PROGRESS)
    assert run.mode == RunMode.MULTI_AGENT and run.completed_at is None
    assert ticket.category == TicketCategory.REFUND
    assert ticket.customer_reply == "Your refund request is awaiting approval."

    proposals = list(
        await session.scalars(select(ActionProposal).where(ActionProposal.run_id == run.id))
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.status == ProposalStatus.PENDING_APPROVAL
    assert proposal.payload["amount_minor"] == PRIYA_AMOUNT  # from the eligibility decision
    approvals = list(
        await session.scalars(select(Approval).where(Approval.proposal_id == proposal.id))
    )
    assert len(approvals) == 1
    assert (
        approvals[0].decision == ApprovalDecision.PENDING
        and approvals[0].payload_hash == proposal.payload_hash
    )

    # Nothing executed: the run created no refund operation (the seed data has its own).
    assert await count(session, RefundOperation) == refunds_before


async def test_the_audit_trail_has_every_step_and_no_content(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    permissive_policy_search: Settings,
) -> None:
    scripts = await priya_scripts(session, permissive_policy_search)
    scripts["supervisor"] = [
        supervisor_reply(
            investigator=True,
            policy=False,  # the model tries to skip a required specialist
            investigator_focus="HINT-TEXT check the return",
        )
    ]
    fake = use_agents(scripts)
    priya = await auth_headers("priya@example.com")

    _, run_id = await start_run(client, priya, "COMPLAINT-TEXT refund please", "ORD-1001")

    rows = await audit_rows(session, run_id)
    types = {row.event_type for row in rows}
    assert types >= {
        "run.created",
        "run.classified",
        "supervisor.decided",
        "investigation.started",
        "tool.called",
        "investigation.completed",
        "policy.completed",
        "resolution.completed",
        "proposal.created",
        "approval.requested",
        "ticket.updated",
        "run.usage",
    }
    assert payloads(rows, "supervisor.decided") == [
        {
            "run_investigator": True,
            "run_policy": True,
            "added_by_code": ["policy"],
            "extra_investigation_round": False,
        }
    ]
    (resolution,) = payloads(rows, "resolution.completed")
    assert resolution["action"] == "refund" and resolution["amount_minor"] == PRIYA_AMOUNT
    assert resolution["reason_codes"] == [] and resolution["failure"] is None
    (policy,) = payloads(rows, "policy.completed")
    assert policy["rules"] == 1 and policy["dropped_rules"] == 0
    assert {p["tool"] for p in payloads(rows, "tool.called")} >= {"get_order", "search_policy"}

    # The hint, the complaint, drafted text and facts are in memory only.
    trail = str([row.redacted_payload for row in rows])
    for secret in ("COMPLAINT-TEXT", "HINT-TEXT", "The order exists", "refund is proposed"):
        assert secret not in trail
    assert fake.requests_by_agent["policy"]  # the policy agent really ran


async def test_the_run_records_its_llm_calls_and_tokens(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    permissive_policy_search: Settings,
) -> None:
    fake = use_agents(await priya_scripts(session, permissive_policy_search))
    priya = await auth_headers("priya@example.com")

    _, run_id = await start_run(client, priya, "refund please", "ORD-1001")

    run = await reload(session, WorkflowRun, run_id)
    assert run.llm_calls == len(fake.requests) > 0
    assert run.input_tokens > 0 and run.output_tokens > 0
    rows = await audit_rows(session, run_id)
    assert payloads(rows, "run.usage") == [
        {
            "llm_calls": run.llm_calls,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
        }
    ]


async def test_a_refund_for_an_order_that_is_not_eligible_creates_no_proposal(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    permissive_policy_search: Settings,
) -> None:
    # Rahul asks two months after delivery. The model drafts a refund anyway; code refuses.
    citation = await policy_citation(
        session, permissive_policy_search, "rahul@example.com", "ORD-1004"
    )
    refund = draft_reply("refund", citations=[citation])
    use_agents(
        {
            "classifier": [classified("refund")],
            "supervisor": [supervisor_reply()],
            "investigator": simple_investigation("ORD-1004"),
            "policy": policy_run(citation, order="ORD-1004"),
            "resolution": [refund, refund],  # the draft and its one revision
            "critic": [critique_reply("approve")],
        }
    )
    rahul = await auth_headers("rahul@example.com")

    refunds_before = await count(session, RefundOperation)

    ticket_id, run_id = await start_run(client, rahul, "I want my money back", "ORD-1004")

    run = await reload(session, WorkflowRun, run_id)
    ticket = await reload(session, Ticket, ticket_id)
    assert (run.status, ticket.status) == (RunStatus.ESCALATED, TicketStatus.ESCALATED)
    assert await count(session, ActionProposal, ActionProposal.run_id == run.id) == 0
    assert await count(session, RefundOperation) == refunds_before
    (resolution,) = payloads(await audit_rows(session, run_id), "resolution.completed")
    assert "REFUND_NOT_ELIGIBLE" in resolution["reason_codes"]


# ---------------------------------------------------------------- baseline mode


async def test_baseline_mode_creates_the_same_pending_approval_with_one_agent(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    branch_sessions: BranchSessions,
    permissive_policy_search: Settings,
) -> None:
    citation = await policy_citation(
        session, permissive_policy_search, "priya@example.com", "ORD-1001"
    )
    fake = AgentFake(
        {
            "classifier": [classified("refund")],
            "baseline": [
                read_call("get_order", "ORD-1001", "call_1"),
                search_call("return window", "ORD-1001", "call_2"),
                text_reply("Done."),
            ],
            "resolution": [draft_reply("refund", call_ids=["call_1"], citations=[citation])],
        }
    )
    priya = await auth_headers("priya@example.com")
    created = await client.post(
        "/tickets", json={"message": "refund please", "order_number": "ORD-1001"}, headers=priya
    )
    ticket_id = UUID(created.json()["id"])
    principal = Principal(
        id=await customer_id(session, "priya@example.com"),
        role=Role.CUSTOMER,
        name="Priya",
        email="priya@example.com",
    )
    shipping_mock = create_shipping_app(ShippingSettings(app_env="test"), clock=lambda: NOW)

    async with ShippingClient(
        permissive_policy_search, transport=httpx.ASGITransport(app=shipping_mock)
    ) as shipping:
        response = await runs_service.start_run(
            session,
            principal,
            ticket_id,
            llm=scripted_client(fake),
            settings=permissive_policy_search,
            shipping_client=shipping,
            embedder=FakeEmbedder(),
            clock=lambda: NOW,
            branch_sessions=branch_sessions,
            mode=RunMode.BASELINE,
        )

    run = await reload(session, WorkflowRun, str(response.run_id))
    assert (run.mode, run.status) == (RunMode.BASELINE, RunStatus.AWAITING_APPROVAL)
    proposals = list(
        await session.scalars(select(ActionProposal).where(ActionProposal.run_id == run.id))
    )
    assert [p.payload["amount_minor"] for p in proposals] == [PRIYA_AMOUNT]
    assert set(fake.requests_by_agent) == {"classifier", "baseline", "resolution"}
    assert run.llm_calls == len(fake.requests)

    rows = await audit_rows(session, str(run.id))
    types = {row.event_type for row in rows}
    assert {"baseline.completed", "resolution.completed", "proposal.created"} <= types
    assert not types & {"supervisor.decided", "investigation.completed", "policy.completed"}
    # Code fetched the shipment record the model never asked for: the same required evidence.
    assert "required_get_shipment" in {p["call_id"] for p in payloads(rows, "tool.called")}


# ---------------------------------------------------------------- the other outcomes


async def test_order_status_is_explained_saved_on_the_ticket_and_completed(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    permissive_policy_search: Settings,
) -> None:
    message = "Your laptop is still with the carrier."
    use_agents(
        {
            "classifier": [classified("order_status")],
            "supervisor": [supervisor_reply(policy=False)],
            "investigator": simple_investigation("ORD-1002"),
            "resolution": [draft_reply("explain_status", message=message)],
            "critic": [critique_reply("approve")],
        }
    )
    arjun = await auth_headers("arjun@example.com")

    ticket_id, run_id = await start_run(client, arjun, "Where is my laptop?", "ORD-1002")

    run = await reload(session, WorkflowRun, run_id)
    ticket = await reload(session, Ticket, ticket_id)
    assert (run.status, ticket.status) == (RunStatus.COMPLETED, TicketStatus.RESOLVED)
    assert run.completed_at is not None
    assert ticket.customer_reply == message
    assert ticket.category == TicketCategory.ORDER_STATUS
    assert await count(session, ActionProposal, ActionProposal.run_id == run.id) == 0
    rows = await audit_rows(session, run_id)
    assert payloads(rows, "policy.completed") == []  # the supervisor skipped it, code allowed it
    assert payloads(rows, "run.completed")[0]["status"] == "completed"


async def test_request_info_saves_the_question_and_waits_for_the_customer(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    permissive_policy_search: Settings,
) -> None:
    question = "Which item in the order arrived damaged?"
    scripts = await priya_scripts(session, permissive_policy_search)
    scripts["resolution"] = [draft_reply("request_info", message=question, citations=[])]
    use_agents(scripts)
    priya = await auth_headers("priya@example.com")

    ticket_id, run_id = await start_run(client, priya, "Something is wrong", "ORD-1001")

    run = await reload(session, WorkflowRun, run_id)
    ticket = await reload(session, Ticket, ticket_id)
    assert (run.status, ticket.status) == (
        RunStatus.AWAITING_CUSTOMER,
        TicketStatus.AWAITING_CUSTOMER,
    )
    assert ticket.customer_reply == question
    assert await count(session, ActionProposal, ActionProposal.run_id == run.id) == 0
    rows = await audit_rows(session, run_id)
    assert payloads(rows, "clarification.requested") == [{"reason": "needs_more_information"}]


async def test_a_draft_that_chooses_escalate_escalates_with_a_fixed_reply(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    permissive_policy_search: Settings,
) -> None:
    scripts = await priya_scripts(session, permissive_policy_search)
    scripts["resolution"] = [draft_reply("escalate", message="A person will look at this.")]
    use_agents(scripts)
    priya = await auth_headers("priya@example.com")

    ticket_id, run_id = await start_run(client, priya, "I want to speak to someone", "ORD-1001")

    run = await reload(session, WorkflowRun, run_id)
    ticket = await reload(session, Ticket, ticket_id)
    assert (run.status, ticket.status) == (RunStatus.ESCALATED, TicketStatus.ESCALATED)
    assert ticket.customer_reply == ESCALATION_MESSAGE
    assert await count(session, ActionProposal, ActionProposal.run_id == run.id) == 0


async def test_llm_failure_while_resolving_escalates_and_creates_nothing(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    permissive_policy_search: Settings,
) -> None:
    scripts = await priya_scripts(session, permissive_policy_search)
    scripts["resolution"] = [transient()]
    use_agents(scripts)
    priya = await auth_headers("priya@example.com")

    ticket_id, run_id = await start_run(client, priya, "refund please", "ORD-1001")

    run = await reload(session, WorkflowRun, run_id)
    ticket = await reload(session, Ticket, ticket_id)
    assert (run.status, ticket.status) == (RunStatus.ESCALATED, TicketStatus.ESCALATED)
    assert await count(session, ActionProposal, ActionProposal.run_id == run.id) == 0
    rows = await audit_rows(session, run_id)
    assert payloads(rows, "run.escalated")[0]["error_codes"] == ["LLM_UNAVAILABLE"]


# ---------------------------------------------------------------- the investigator still audits


async def test_another_customers_order_is_not_found_and_leaks_nothing(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    permissive_policy_search: Settings,
) -> None:
    # Rahul's ticket is about ORD-1004. A manipulated model also asks for Priya's ORD-1001,
    # then writes a "fact" from that failed lookup.
    fake = use_agents(
        {
            "classifier": [classified("order_status")],
            "supervisor": [supervisor_reply(policy=False)],
            "investigator": [
                plan_reply(),
                read_call("get_order", "ORD-1001", "call_foreign"),
                read_call("get_order", "ORD-1004", "call_own"),
                text_reply("Done."),
                summary_reply(
                    [
                        ("FOREIGN the order contains headphones.", "call_foreign"),
                        ("OWN the order was delivered.", "call_own"),
                    ]
                ),
            ],
            "resolution": [
                draft_reply(
                    "explain_status", message="Your order was delivered.", call_ids=["call_own"]
                )
            ],
            "critic": [critique_reply("approve")],
        }
    )
    rahul = await auth_headers("rahul@example.com")

    _, run_id = await start_run(client, rahul, "Where is my order?", "ORD-1004")

    rows = await audit_rows(session, run_id)
    calls = {p["call_id"]: p for p in payloads(rows, "tool.called")}
    assert calls["call_foreign"]["outcome"] == "ORDER_NOT_FOUND"
    assert "order_number" not in calls["call_foreign"]  # a failed lookup is not recorded
    assert (calls["call_own"]["outcome"], calls["call_own"]["order_number"]) == ("ok", "ORD-1004")
    completed = payloads(rows, "investigation.completed")[0]
    assert (completed["dropped_facts"], completed["failed_calls"]) == (1, 1)

    shown = "\n".join(m.content for request in fake.requests for m in request.messages)
    for foreign in ("Wireless Headphones", "249900", "SKU-HP-200"):
        assert foreign not in shown
    assert "ORD-1001" not in str([row.redacted_payload for row in rows])
