"""A run's live status: the snapshot behind GET /runs/{id}/events and GET /runs/{id}/stream.

Ids, codes and timestamps only, plus the ticket's verified customer reply (ADR-019).
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from support_agent.domain.enums import RunStatus, TicketStatus


class RunStatusEvent(BaseModel):
    run_id: UUID
    ticket_id: UUID
    status: RunStatus
    ticket_status: TicketStatus
    # The latest message the run gave the customer. Checked by the output guardrail and
    # stating only verified outcomes; None until the run has written one.
    reply: str | None
    # True when nothing more will happen on its own (clients stop listening).
    finished: bool


class RunStepEvent(BaseModel):
    id: UUID  # the audit event's id: clients use it to skip events they already have
    type: str  # e.g. "approval.requested"
    actor: str  # a code: "customer", "reviewer", "system", ...
    at: datetime
    # Reviewers only: allowlisted payload fields (ids, codes, counts). Always {} for customers.
    details: dict[str, str | int | float | bool | list[str | int | float | bool]]


class RunSnapshot(BaseModel):
    run: RunStatusEvent
    steps: list[RunStepEvent]  # oldest first
