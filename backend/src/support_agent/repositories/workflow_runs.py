"""All SQL for workflow runs."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import WorkflowRun
from support_agent.domain.enums import RunMode, RunStatus

# A run in one of these states is over, so it gets a completed_at time.
FINISHED_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.ESCALATED, RunStatus.FAILED})


async def create_manual(
    session: AsyncSession, *, ticket_id: UUID, status: RunStatus, now: datetime
) -> WorkflowRun:
    """A reviewer-driven run. It has no LangGraph thread, but thread_id must be unique."""
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        ticket_id=ticket_id,
        thread_id=f"manual-{run_id}",
        mode=RunMode.MANUAL,
        status=status,
        started_at=now,
    )
    session.add(run)
    await session.flush()
    return run


async def create_agent_run(
    session: AsyncSession, *, ticket_id: UUID, mode: RunMode, now: datetime
) -> WorkflowRun:
    """A run driven by the LangGraph workflow. thread_id is its checkpointer key (M7)."""
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        ticket_id=ticket_id,
        thread_id=f"run-{run_id}",
        mode=mode,
        status=RunStatus.CREATED,
        started_at=now,
    )
    session.add(run)
    await session.flush()
    return run


async def has_unfinished_run(session: AsyncSession, ticket_id: UUID) -> bool:
    """True if the ticket has a run that is not completed, escalated or failed."""
    stmt = select(
        exists().where(
            WorkflowRun.ticket_id == ticket_id,
            WorkflowRun.status.not_in(FINISHED_STATUSES),
        )
    )
    return bool(await session.scalar(stmt))


async def get(session: AsyncSession, run_id: UUID) -> WorkflowRun | None:
    return await session.get(WorkflowRun, run_id)


async def set_status(
    session: AsyncSession, run: WorkflowRun, status: RunStatus, now: datetime
) -> None:
    run.status = status
    if status in FINISHED_STATUSES:
        run.completed_at = now
    await session.flush()


async def set_usage(
    session: AsyncSession, run: WorkflowRun, *, calls: int, input_tokens: int, output_tokens: int
) -> None:
    run.llm_calls = calls
    run.input_tokens = input_tokens
    run.output_tokens = output_tokens
    await session.flush()
