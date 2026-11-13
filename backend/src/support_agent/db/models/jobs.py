import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UpdatedAt, UUIDPrimaryKey, str_enum
from support_agent.domain.enums import JobStatus, JobType


class Job(UUIDPrimaryKey, CreatedAt, UpdatedAt, Base):
    """A unit of background work for the Postgres job queue (worker/queue.py, ADR-012).

    Workers claim jobs with SELECT ... FOR UPDATE SKIP LOCKED, so two workers never take the
    same job. A claimed job holds a lease; a worker that dies stops renewing it, and once
    the lease has run out another worker may claim the job again.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        # Every job works on one run, except the scheduled approval-expiry sweep.
        CheckConstraint("run_id IS NOT NULL OR job_type = 'expire_approvals'", name="run_required"),
        # Serves the worker's query: "queued jobs whose available_at has passed".
        Index("ix_jobs_status_available_at", "status", "available_at"),
        # At most one approval-expiry sweep waiting or running, however many workers
        # schedule one at the same moment.
        Index(
            "uq_jobs_one_open_expiry_sweep",
            "job_type",
            unique=True,
            postgresql_where=text(
                "job_type = 'expire_approvals' AND status IN ('queued', 'running')"
            ),
        ),
    )

    job_type: Mapped[JobType] = mapped_column(str_enum(JobType, "job_type"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    # Ids only (a message id, an interrupt id). Never customer text: jobs are not private.
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, "status"),
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
    )
    # Incremented on every claim. It is also the "fencing token": a worker may finish a job
    # only while the attempt number is still the one it claimed (ADR-012).
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(default=5, server_default="5")
    # Earliest time a worker may pick the job up; pushed forward for retry backoff.
    available_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # While RUNNING: when the claim runs out unless the worker's heartbeat renews it.
    lease_expires_at: Mapped[datetime | None]
    # A code only (an AppError code or an exception class name), never a message.
    last_error_code: Mapped[str | None] = mapped_column(String(64))
