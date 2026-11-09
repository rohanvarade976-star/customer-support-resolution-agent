"""Tickets, workflow runs and long-term customer memory."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UpdatedAt, UUIDPrimaryKey, str_enum
from support_agent.domain.enums import RunMode, RunStatus, TicketCategory, TicketStatus


class Ticket(UUIDPrimaryKey, CreatedAt, UpdatedAt, Base):
    __tablename__ = "tickets"
    __table_args__ = (
        # If a ticket names an order, that order must belong to the same customer.
        # (A NULL order_id skips this check: not every complaint names an order.)
        ForeignKeyConstraint(["order_id", "customer_id"], ["orders.id", "orders.customer_id"]),
        # Lets customer_memory reference (ticket id, owner) together; see below.
        UniqueConstraint("id", "customer_id"),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    # NULL until the classifier has labelled the complaint.
    category: Mapped[TicketCategory | None] = mapped_column(str_enum(TicketCategory, "category"))
    status: Mapped[TicketStatus] = mapped_column(
        str_enum(TicketStatus, "status"),
        default=TicketStatus.OPEN,
        server_default=TicketStatus.OPEN.value,
    )
    summary: Mapped[str] = mapped_column(Text)


class WorkflowRun(UUIDPrimaryKey, UpdatedAt, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at", name="completed_after_started"
        ),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"), index=True)
    # The LangGraph checkpointer thread for this run (pause/resume key).
    thread_id: Mapped[str] = mapped_column(String(64), unique=True)
    mode: Mapped[RunMode] = mapped_column(
        str_enum(RunMode, "mode"),
        default=RunMode.MULTI_AGENT,
        server_default=RunMode.MULTI_AGENT.value,
    )
    status: Mapped[RunStatus] = mapped_column(
        str_enum(RunStatus, "status"),
        default=RunStatus.CREATED,
        server_default=RunStatus.CREATED.value,
    )
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]


class CustomerMemory(UUIDPrimaryKey, CreatedAt, Base):
    """Summary of a past ticket, loaded as context only, never as facts (FR-21)."""

    __tablename__ = "customer_memory"
    __table_args__ = (
        # The ticket must belong to the same customer, so one customer's memory
        # can never be filed under (and later shown to) another customer.
        ForeignKeyConstraint(["ticket_id", "customer_id"], ["tickets.id", "tickets.customer_id"]),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(index=True)
    summary: Mapped[str] = mapped_column(Text)
