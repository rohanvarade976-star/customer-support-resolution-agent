"""A run's live status (FR-22, ADR-019). The owner or staff only; anyone else gets 404.

- GET /runs/{id}/events: the snapshot as JSON (the client's polling fallback).
- GET /runs/{id}/stream: the same as Server-Sent Events, then each change as it happens.

The token comes in the Authorization header, never the URL: the browser reads the stream
with fetch(), because EventSource cannot send headers.
"""

from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from support_agent.api.deps import (
    PrincipalDep,
    SessionDep,
    SessionFactoryDep,
    SettingsDep,
    ShortSessionDep,
    StreamingPrincipalDep,
)
from support_agent.schemas.run_events import RunSnapshot
from support_agent.services import run_events as run_events_service

router = APIRouter(tags=["runs"])

# no-cache: never stored by a proxy. X-Accel-Buffering: nginx-style proxies pass each
# event on at once instead of buffering the response.
_STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.get("/runs/{run_id}/events")
async def get_run_events(run_id: UUID, principal: PrincipalDep, session: SessionDep) -> RunSnapshot:
    return await run_events_service.load_snapshot(session, principal, run_id)


@router.get("/runs/{run_id}/stream", response_class=StreamingResponse)
async def stream_run_events(
    run_id: UUID,
    principal: StreamingPrincipalDep,
    session: ShortSessionDep,
    sessions: SessionFactoryDep,
    settings: SettingsDep,
) -> StreamingResponse:
    # Checked here, before any byte is sent, so a stranger's run is a plain 404.
    first = await run_events_service.load_snapshot(session, principal, run_id)
    events = run_events_service.stream_run(sessions, principal, first, settings)
    return StreamingResponse(events, media_type="text/event-stream", headers=_STREAM_HEADERS)
