import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import UUIDPrimaryKey


class AuditEvent(UUIDPrimaryKey, Base):
    """Append-only record of who did what. Payloads are redacted before they are stored."""

    __tablename__ = "audit_events"

    # clock_timestamp(), not now(): now() is the transaction's start time, so every event written
    # in one transaction would share it and their order would be lost. clock_timestamp() is read
    # at each insert, so events keep the order they were written in (ADR-019).
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), sort_order=100
    )

    # NULL for events outside a workflow run (e.g. a reviewer opening the queue).
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    actor: Mapped[str] = mapped_column(String(64))  # e.g. "customer", "reviewer", "system"
    event_type: Mapped[str] = mapped_column(String(64))  # e.g. "approval.approved"
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
