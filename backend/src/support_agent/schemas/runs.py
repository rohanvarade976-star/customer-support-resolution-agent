"""Workflow run response model."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from support_agent.domain.enums import JobStatus, JobType, RunStatus, TicketCategory


class RunJob(BaseModel):
    """The run's newest background job: how far the queued work has got.

    `queued` with attempts > 0 means it failed and waits for a retry at `next_attempt_at`.
    Error codes are not shown here; they are for staff, in the database and the logs.
    """

    job_type: JobType
    status: JobStatus
    attempts: int
    next_attempt_at: datetime | None  # set while queued


class RunResponse(BaseModel):
    run_id: UUID
    ticket_id: UUID
    status: RunStatus
    category: TicketCategory | None  # the ticket's category; None until classified
    started_at: datetime
    completed_at: datetime | None
    job: RunJob | None = None  # None for manual (M3) runs, which have no jobs
