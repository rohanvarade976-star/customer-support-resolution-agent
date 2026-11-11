"""All SQL for the audit trail. Rows are only ever added, never changed (FR-20).

Callers pass payloads built from ids, amounts and status codes only. Names, emails
and customer message text never go in here.
"""

from typing import Any
from uuid import UUID

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
