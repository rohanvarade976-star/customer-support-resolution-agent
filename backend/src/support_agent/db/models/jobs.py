import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UpdatedAt, UUIDPrimaryKey, str_enum
from support_agent.domain.enums import JobStatus


class Job(UUIDPrimaryKey, CreatedAt, UpdatedAt, Base):
    """A unit of background work for the Postgres job queue (worker comes later).

    Workers claim jobs with SELECT ... FOR UPDATE SKIP LOCKED, so two workers
    never take the same job.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        # Serves the worker's query: "queued jobs whose available_at has passed".
        Index("ix_jobs_status_available_at", "status", "available_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, "status"),
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    # Earliest time a worker may pick the job up; pushed forward for retry backoff.
    available_at: Mapped[datetime] = mapped_column(server_default=func.now())
    locked_at: Mapped[datetime | None]  # when a worker claimed it; spots stuck jobs
    last_error: Mapped[str | None] = mapped_column(Text)
