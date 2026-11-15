"""A run's live status: the snapshot behind GET /runs/{id}/events and GET /runs/{id}/stream.

Ids, codes and timestamps only, plus the ticket's verified customer reply (ADR-019).
"""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from support_agent.domain.enums import (
    OrderStatus,
    ReplyAuthor,
    ReturnStatus,
    RunStatus,
    TicketStatus,
)

# What a customer may be told about a refund: coarse and final-or-not, never an amount or code.
CustomerRefundStatus = Literal["in_progress", "succeeded", "failed"]


class ShipmentFacts(BaseModel):
    """The carrier's own report for an order in transit."""

    status: Literal["in_transit", "delivered"]
    expected_delivery: date


class RunFacts(BaseModel):
    """Verified facts about the ticket's order, read from our records now (ADR-023).

    Owner-scoped (the order is looked up by the ticket's customer), and only fixed values:
    no model text, no internal ids or codes, no amounts.
    """

    order_number: str
    order_status: OrderStatus
    delivered_at: datetime | None
    return_status: ReturnStatus | None  # the latest return, if any
    refund_status: CustomerRefundStatus | None  # this run's refund, if one exists
    shipment: ShipmentFacts | None  # only while the order is in transit


class RunStatusEvent(BaseModel):
    run_id: UUID
    ticket_id: UUID
    status: RunStatus
    ticket_status: TicketStatus
    # The latest message the run gave the customer. Checked by the output guardrail and
    # stating only verified outcomes; None until the run has written one.
    reply: str | None
    # Who wrote it: the assistant, or a specialist answering an escalated ticket (ADR-022).
    reply_from: ReplyAuthor = ReplyAuthor.ASSISTANT
    # True when nothing more will happen on its own (clients stop listening).
    finished: bool
    # The kinds of records the run checked successfully, as fixed codes from
    # domain/run_events.CHECKED_SOURCES ("order", "return", "payment", "shipment", "policy").
    # Never tool names, results or model text: safe for the customer (ADR-021).
    checked: list[str] = []
    # Verified facts for the "What we found" panel; None when the ticket has no order.
    facts: RunFacts | None = None
    # The expectation shown while a specialist has the ticket (a setting, not a promise).
    specialist_reply_business_days: int = 1


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
