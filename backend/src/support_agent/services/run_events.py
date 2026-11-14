"""A run's live status for the customer timeline and the reviewer view (FR-22, ADR-019).

`load_snapshot` builds the whole picture once (the polling fallback, and the first thing a
stream sends). `stream_run` then polls the database and sends only what changed.

Each poll opens and closes its own short session. A stream never holds a transaction or a
row lock while it waits (CLAUDE.md rule 7), and it checks ownership again on every poll.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.db.models import AuditEvent
from support_agent.domain.enums import Role
from support_agent.domain.run_events import (
    compact_json,
    is_finished,
    reviewer_details,
    safe_actor,
    sse_comment,
    sse_frame,
    visible_to_customer,
)
from support_agent.repositories import audit
from support_agent.schemas.auth import Principal
from support_agent.schemas.run_events import RunSnapshot, RunStatusEvent, RunStepEvent
from support_agent.services.runs import load_visible_run

# Opens a short-lived session: `async with sessions() as session: ...`
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
Monotonic = Callable[[], float]


def _step(event: AuditEvent, reviewer: bool) -> RunStepEvent:
    return RunStepEvent(
        id=event.id,
        type=event.event_type,
        actor=safe_actor(event.actor),
        at=event.created_at,
        details=reviewer_details(event.redacted_payload) if reviewer else {},
    )


async def load_snapshot(session: AsyncSession, principal: Principal, run_id: UUID) -> RunSnapshot:
    """The run's status, verified reply and steps, as this caller may see them."""
    run, ticket = await load_visible_run(session, principal, run_id)  # 404 if not theirs
    reviewer = principal.role in (Role.REVIEWER, Role.ADMIN)
    events = await audit.list_for_run(session, run.id)
    steps = [
        _step(event, reviewer)
        for event in events
        if reviewer or visible_to_customer(event.event_type)
    ]
    status = RunStatusEvent(
        run_id=run.id,
        ticket_id=ticket.id,
        status=run.status,
        ticket_status=ticket.status,
        reply=ticket.customer_reply,
        finished=is_finished(run.status, ticket.status),
    )
    return RunSnapshot(run=status, steps=steps)


def _status_frame(status: RunStatusEvent) -> str:
    return sse_frame("status", status.model_dump_json())


def _step_frame(step: RunStepEvent) -> str:
    return sse_frame("step", step.model_dump_json(), event_id=str(step.id))


def _end_frame(reason: str) -> str:
    # "finished": stop listening. "timeout": reconnect for a fresh stream.
    return sse_frame("end", compact_json({"reason": reason}))


async def stream_run(
    sessions: SessionFactory,
    principal: Principal,
    first: RunSnapshot,
    settings: Settings,
    monotonic: Monotonic = time.monotonic,
) -> AsyncGenerator[str]:
    """Server-Sent Events for one run: the snapshot first, then each change as it happens.

    Ends when the run is finished, or after `sse_max_stream_seconds` (the client then
    reconnects with its token). A client that disconnects stops the generator.
    """
    seen: set[UUID] = set()
    yield _status_frame(first.run)
    for step in first.steps:
        seen.add(step.id)
        yield _step_frame(step)
    if first.run.finished:
        yield _end_frame("finished")
        return

    last_status = first.run
    deadline = monotonic() + settings.sse_max_stream_seconds
    last_sent = monotonic()
    while monotonic() < deadline:
        await asyncio.sleep(settings.sse_poll_interval_seconds)
        async with sessions() as session:
            snapshot = await load_snapshot(session, principal, first.run.run_id)

        frames = [_step_frame(step) for step in snapshot.steps if step.id not in seen]
        seen.update(step.id for step in snapshot.steps)
        if snapshot.run != last_status:
            frames.append(_status_frame(snapshot.run))
            last_status = snapshot.run

        if frames:
            for frame in frames:
                yield frame
            last_sent = monotonic()
        elif monotonic() - last_sent >= settings.sse_heartbeat_seconds:
            yield sse_comment("keep-alive")
            last_sent = monotonic()

        if snapshot.run.finished:
            yield _end_frame("finished")
            return
    yield _end_frame("timeout")
