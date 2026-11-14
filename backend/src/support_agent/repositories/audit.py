"""All SQL for the audit trail. Rows are only ever added, never changed (FR-20).

Callers pass payloads built from ids, amounts and status codes only. Names, emails
and customer message text never go in here.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import AuditEvent


async def record(
    session: AsyncSession,
    *,
    run_id: UUID | None,
    actor: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        AuditEvent(run_id=run_id, actor=actor, event_type=event_type, redacted_payload=payload)
    )
    await session.flush()


# A run writes a few dozen events; this cap only guards against a runaway loop.
MAX_EVENTS_PER_RUN = 500


async def list_for_run(session: AsyncSession, run_id: UUID) -> list[AuditEvent]:
    """A run's events, oldest first (id breaks ties between events of one transaction)."""
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.run_id == run_id)
        .order_by(AuditEvent.created_at, AuditEvent.id)
        .limit(MAX_EVENTS_PER_RUN)
    )
    return list((await session.scalars(stmt)).all())
