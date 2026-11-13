"""The investigator end to end: POST a run, real tools and services, real database.

The LLM is the scripted fake (never a real API) and shipping is the in-memory mock.
The tests check the database (audit events, run and ticket status) and what the model
was shown, not the wording of any reply.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agents.fake import plan_reply, read_call, summary_reply
from support_agent.db.models import AuditEvent, Ticket, WorkflowRun
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import RunStatus, TicketStatus
from support_agent.llm.fake import FakeProvider, ScriptStep, text_reply, tool_reply, transient

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseLLM = Callable[[list[ScriptStep]], FakeProvider]
TOOL_CALLED_KEYS = {"tool", "call_id", "outcome", "latency_ms", "order_number"}


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str, ingested_policies: None) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


def classified(category: str = "refund") -> ScriptStep:
    return tool_reply(
        "classification", {"category": category, "confidence": 0.95, "reason": "test"}
    )


async def start_run(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    message: str,
    order: str | None,
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


async def audit_rows(session: AsyncSession, run_id: str) -> list[AuditEvent]:
    return list(await session.scalars(select(AuditEvent).where(AuditEvent.run_id == UUID(run_id))))


def payloads(rows: list[AuditEvent], event_type: str) -> list[dict[str, Any]]:
    return [row.redacted_payload for row in rows if row.event_type == event_type]


def everything_shown_to_the_model(fake: FakeProvider) -> str:
    return "\n".join(m.content for request in fake.requests for m in request.messages)


# ---------------------------------------------------------------- Priya: a real multi-step run


async def test_priya_investigation_runs_real_tools_and_audits_without_content(
    client: httpx.AsyncClient, session: AsyncSession, auth_headers: HeadersFor, use_llm: UseLLM
) -> None:
    fake = use_llm(
        [
            classified(),
            plan_reply("Find out if the refund can go ahead.", ["Return", "Payment", "Rules"]),
            read_call("get_return_status", "ORD-1001", "call_a"),
            read_call("get_payment_status", "ORD-1001", "call_b"),
            read_call("check_refund_eligibility", "ORD-1001", "call_c"),
            text_reply("Done."),
            summary_reply(
                [
                    ("FACT-ONE the return was received.", "call_a"),
                    ("FACT-TWO the order was paid.", "call_b"),
                    ("FACT-THREE the order is eligible.", "call_c"),
                    ("FACT-INVENTED made up.", "call_not_made"),
                ]
            ),
        ]
    )
    priya = await auth_headers("priya@example.com")

    ticket_id, run_id = await start_run(
        client, priya, "COMPLAINT-TEXT I returned my headphones. Where is my refund?", "ORD-1001"
    )

    run = await session.get(WorkflowRun, UUID(run_id))
    ticket = await session.get(Ticket, UUID(ticket_id))
    assert run is not None and ticket is not None
    assert (run.status, ticket.status) == (RunStatus.INVESTIGATING, TicketStatus.IN_PROGRESS)

    # The real services ran: their results (Priya's paid amount) reached the model.
    assert "249900" in everything_shown_to_the_model(fake)

    rows = await audit_rows(session, run_id)
    calls = {p["call_id"]: p for p in payloads(rows, "tool.called")}
    assert {k: (v["tool"], v["outcome"]) for k, v in calls.items()} == {
        "call_a": ("get_return_status", "ok"),
        "call_b": ("get_payment_status", "ok"),
        "call_c": ("check_refund_eligibility", "ok"),
    }
    assert all(
        set(p) == TOOL_CALLED_KEYS and p["order_number"] == "ORD-1001" for p in calls.values()
    )
    assert payloads(rows, "investigation.started") == [{"run_id": run_id}]
    assert payloads(rows, "investigation.completed") == [
        {
            "tool_calls": 3,
            "failed_calls": 0,
            "facts": 3,
            "dropped_facts": 1,
            "missing_information": 0,
        }
    ]

    # No content in the audit trail: no complaint, plan, fact text or tool result.
    trail = str([row.redacted_payload for row in rows])
    for secret in ("COMPLAINT-TEXT", "FACT-", "Find out if", "Wireless Headphones", "249900"):
        assert secret not in trail


# ---------------------------------------------------------------- another customer's order


async def test_asking_for_another_customers_order_returns_not_found_and_leaks_nothing(
    client: httpx.AsyncClient, session: AsyncSession, auth_headers: HeadersFor, use_llm: UseLLM
) -> None:
    # Rahul's ticket is about ORD-1004. A manipulated model also asks for Priya's ORD-1001,
    # then writes a "fact" from that failed lookup.
    fake = use_llm(
        [
            classified(),
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
        ]
    )
    rahul = await auth_headers("rahul@example.com")

    _, run_id = await start_run(client, rahul, "Where is my order?", "ORD-1004")

    rows = await audit_rows(session, run_id)
    calls = {p["call_id"]: p for p in payloads(rows, "tool.called")}
    assert calls["call_foreign"]["outcome"] == "ORDER_NOT_FOUND"
    assert "order_number" not in calls["call_foreign"]  # a failed lookup is not recorded
    assert (calls["call_own"]["outcome"], calls["call_own"]["order_number"]) == ("ok", "ORD-1004")
    completed = payloads(rows, "investigation.completed")[0]
    assert (completed["facts"], completed["dropped_facts"], completed["failed_calls"]) == (1, 1, 1)

    # Nothing about Priya's order reached the model or the audit trail.
    shown = everything_shown_to_the_model(fake)
    for foreign in ("Wireless Headphones", "249900", "SKU-HP-200"):
        assert foreign not in shown
    assert "ORD-1001" not in str([row.redacted_payload for row in rows])


# ---------------------------------------------------------------- failures escalate


async def test_step_limit_escalates_the_run_and_audits_every_call(
    client: httpx.AsyncClient, session: AsyncSession, auth_headers: HeadersFor, use_llm: UseLLM
) -> None:
    # The default limit is 8 calls; the 9th request is refused before it runs.
    endless = [read_call("get_order", "ORD-1001", f"call_{n}") for n in range(9)]
    use_llm([classified(), plan_reply(), *endless])
    priya = await auth_headers("priya@example.com")

    ticket_id, run_id = await start_run(client, priya, "Where is my order?", "ORD-1001")

    run = await session.get(WorkflowRun, UUID(run_id))
    ticket = await session.get(Ticket, UUID(ticket_id))
    assert run is not None and ticket is not None
    assert (run.status, ticket.status) == (RunStatus.ESCALATED, TicketStatus.ESCALATED)
    rows = await audit_rows(session, run_id)
    assert len(payloads(rows, "tool.called")) == 8
    assert payloads(rows, "investigation.completed") == []
    assert payloads(rows, "run.escalated")[0]["error_codes"] == ["INVESTIGATION_STEP_LIMIT"]


async def test_llm_failure_midway_escalates_and_keeps_the_calls_made(
    client: httpx.AsyncClient, session: AsyncSession, auth_headers: HeadersFor, use_llm: UseLLM
) -> None:
    use_llm([classified(), plan_reply(), read_call("get_order", "ORD-1001", "call_a"), transient()])
    priya = await auth_headers("priya@example.com")

    _, run_id = await start_run(client, priya, "Where is my order?", "ORD-1001")

    run = await session.get(WorkflowRun, UUID(run_id))
    assert run is not None and run.status == RunStatus.ESCALATED
    rows = await audit_rows(session, run_id)
    assert [p["call_id"] for p in payloads(rows, "tool.called")] == ["call_a"]
    assert payloads(rows, "run.escalated")[0]["error_codes"] == ["LLM_UNAVAILABLE"]
    assert payloads(rows, "investigation.completed") == []


async def test_confident_label_without_an_order_asks_the_customer_and_skips_the_agent(
    client: httpx.AsyncClient, session: AsyncSession, auth_headers: HeadersFor, use_llm: UseLLM
) -> None:
    fake = use_llm([classified()])  # only the classifier answers; the investigator would fail
    priya = await auth_headers("priya@example.com")

    _, run_id = await start_run(client, priya, "Where is my refund?", None)

    run = await session.get(WorkflowRun, UUID(run_id))
    assert run is not None and run.status == RunStatus.AWAITING_CUSTOMER
    assert len(fake.requests) == 1
    rows = await audit_rows(session, run_id)
    assert payloads(rows, "clarification.requested") == [{"reason": "missing_order_number"}]
    assert payloads(rows, "investigation.started") == []
