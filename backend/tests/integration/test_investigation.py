"""The investigator's failure paths inside the full workflow: POST a run, real database.

The LLM is the scripted `AgentFake` (never a real API) and shipping is the in-memory mock.
The tests check the database (audit events, run and ticket status), not the wording of any
reply. The successful multi-agent paths and the audit trail are in test_workflow_outcomes.py.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agents.fake import AgentFake, plan_reply, read_call, supervisor_reply
from support_agent.db.models import AuditEvent, Ticket, WorkflowRun
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import RunStatus, TicketStatus
from support_agent.llm.fake import ScriptStep, tool_reply, transient

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseAgents = Callable[[dict[str, list[ScriptStep]]], AgentFake]


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str, ingested_policies: None) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


def classified(category: str = "order_status") -> ScriptStep:
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


async def test_step_limit_escalates_the_run_and_audits_every_call(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
) -> None:
    # The default limit is 8 calls; the 9th request is refused before it runs.
    endless = [read_call("get_order", "ORD-1001", f"call_{n}") for n in range(9)]
    use_agents(
        {
            "classifier": [classified()],
            "supervisor": [supervisor_reply(policy=False)],
            "investigator": [plan_reply(), *endless],
        }
    )
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
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
) -> None:
    use_agents(
        {
            "classifier": [classified()],
            "supervisor": [supervisor_reply(policy=False)],
            "investigator": [
                plan_reply(),
                read_call("get_order", "ORD-1001", "call_a"),
                transient(),
            ],
        }
    )
    priya = await auth_headers("priya@example.com")

    _, run_id = await start_run(client, priya, "Where is my order?", "ORD-1001")

    run = await session.get(WorkflowRun, UUID(run_id))
    assert run is not None and run.status == RunStatus.ESCALATED
    rows = await audit_rows(session, run_id)
    assert [p["call_id"] for p in payloads(rows, "tool.called")] == ["call_a"]
    assert payloads(rows, "run.escalated")[0]["error_codes"] == ["LLM_UNAVAILABLE"]
    assert payloads(rows, "investigation.completed") == []


async def test_confident_label_without_an_order_asks_the_customer_and_skips_the_agents(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
) -> None:
    fake = use_agents({"classifier": [classified("refund")]})  # nothing else is scripted
    priya = await auth_headers("priya@example.com")

    _, run_id = await start_run(client, priya, "Where is my refund?", None)

    run = await session.get(WorkflowRun, UUID(run_id))
    assert run is not None and run.status == RunStatus.AWAITING_CUSTOMER
    assert len(fake.requests) == 1
    rows = await audit_rows(session, run_id)
    assert payloads(rows, "clarification.requested") == [{"reason": "missing_order_number"}]
    assert payloads(rows, "investigation.started") == []
    assert payloads(rows, "supervisor.decided") == []
