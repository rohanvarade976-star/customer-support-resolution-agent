"""Two "start a run" requests at the same moment create exactly one run (M5's open race).

The service first asks "does this ticket have an unfinished run?". Two requests can both
hear "no" before either inserts. The partial unique index on workflow_runs then lets only
one insert through. The requests use separate connections with real commits
(`committed_ticket`), as two API requests would.
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_agent.core.config import get_settings
from support_agent.core.errors import AppError
from support_agent.db.models import Job, WorkflowRun
from support_agent.domain.enums import Role
from support_agent.repositories import workflow_runs
from support_agent.schemas.auth import Principal
from support_agent.schemas.runs import RunResponse
from support_agent.services import runs as runs_service
from support_agent.workflow.graph import Checkpointer

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


async def _start_twice_at_once(
    sessions: async_sessionmaker[AsyncSession],
    checkpointer: Checkpointer,
    customer_id: UUID,
    ticket_id: UUID,
) -> list[RunResponse | BaseException]:
    principal = Principal(id=customer_id, role=Role.CUSTOMER, name="C", email="c@example.com")
    env = runs_service.EnqueueEnv(get_settings(), lambda: NOW, checkpointer)

    async def start() -> RunResponse:
        async with sessions() as db:
            return await runs_service.start_run(db, principal, ticket_id, env)

    return list(await asyncio.gather(start(), start(), return_exceptions=True))


async def _count(sessions: async_sessionmaker[AsyncSession], model: type, ticket_id: UUID) -> int:
    runs = select(WorkflowRun.id).where(WorkflowRun.ticket_id == ticket_id)
    where = WorkflowRun.id.in_(runs) if model is WorkflowRun else Job.run_id.in_(runs)
    async with sessions() as db:
        return await db.scalar(select(func.count()).select_from(model).where(where)) or 0


def _one_run_one_refusal(results: list[RunResponse | BaseException]) -> None:
    started = [r for r in results if isinstance(r, RunResponse)]
    refused = [r for r in results if isinstance(r, AppError)]
    assert len(started) == 1, results
    assert [e.error_code for e in refused] == ["RUN_ALREADY_ACTIVE"]


async def test_concurrent_starts_create_one_run(
    committed_sessions: async_sessionmaker[AsyncSession],
    committed_ticket: tuple[UUID, UUID],
    checkpointer: Checkpointer,
) -> None:
    customer_id, ticket_id = committed_ticket

    results = await _start_twice_at_once(committed_sessions, checkpointer, customer_id, ticket_id)

    _one_run_one_refusal(results)
    assert await _count(committed_sessions, WorkflowRun, ticket_id) == 1
    assert await _count(committed_sessions, Job, ticket_id) == 1  # and one job for it


async def test_the_database_refuses_the_second_run_when_both_pass_the_check(
    committed_sessions: async_sessionmaker[AsyncSession],
    committed_ticket: tuple[UUID, UUID],
    checkpointer: Checkpointer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worst interleaving, forced: both requests are told "no unfinished run"."""

    async def nothing_unfinished(_session: AsyncSession, _ticket_id: UUID) -> bool:
        return False

    monkeypatch.setattr(workflow_runs, "has_unfinished_run", nothing_unfinished)
    customer_id, ticket_id = committed_ticket

    results = await _start_twice_at_once(committed_sessions, checkpointer, customer_id, ticket_id)

    _one_run_one_refusal(results)
    assert await _count(committed_sessions, WorkflowRun, ticket_id) == 1
    assert await _count(committed_sessions, Job, ticket_id) == 1
