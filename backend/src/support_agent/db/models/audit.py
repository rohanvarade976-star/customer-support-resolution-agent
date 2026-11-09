import uuid
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UUIDPrimaryKey


class AuditEvent(UUIDPrimaryKey, CreatedAt, Base):
    """Append-only record of who did what. Payloads are redacted before they are stored."""

    __tablename__ = "audit_events"

    # NULL for events outside a workflow run (e.g. a reviewer opening the queue).
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    actor: Mapped[str] = mapped_column(String(64))  # e.g. "customer", "reviewer", "system"
    event_type: Mapped[str] = mapped_column(String(64))  # e.g. "approval.approved"
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
