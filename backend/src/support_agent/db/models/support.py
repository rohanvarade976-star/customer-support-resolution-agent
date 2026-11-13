"""Tickets, workflow runs, customers' replies to runs, and long-term customer memory."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UpdatedAt, UUIDPrimaryKey, str_enum
from support_agent.domain.enums import (
    MemoryKind,
    MemoryOutcome,
    RunMode,
    RunStatus,
    TicketCategory,
    TicketStatus,
)

# A run in one of these states is over. Kept as SQL here because the partial index below
# needs it; repositories/workflow_runs.py has the same set as Python values.
_FINISHED_RUN_SQL = "status NOT IN ('completed', 'escalated', 'failed')"


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
    # The latest message shown to the customer by a run (an explanation, a question, or a
    # fixed "awaiting approval" / "passed to our team" line). NULL until a run sets one.
    customer_reply: Mapped[str | None] = mapped_column(Text)


class WorkflowRun(UUIDPrimaryKey, UpdatedAt, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at", name="completed_after_started"
        ),
        CheckConstraint(
            "llm_calls >= 0 AND input_tokens >= 0 AND output_tokens >= 0",
            name="usage_non_negative",
        ),
        # At most one unfinished run per ticket. Two "start a run" requests at the same
        # moment both pass the service's check; the database refuses the second insert.
        Index(
            "uq_workflow_runs_one_unfinished_per_ticket",
            "ticket_id",
            unique=True,
            postgresql_where=text(_FINISHED_RUN_SQL),
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
    # What the run's LLM calls cost: successful calls and their tokens (llm/usage.py).
    llm_calls: Mapped[int] = mapped_column(default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(default=0, server_default="0")


class CustomerMessage(UUIDPrimaryKey, CreatedAt, Base):
    """A customer's reply to a run's question (POST /runs/{id}/messages).

    Stored so the background job can carry only the message id. The body is untrusted
    customer text: it is never copied into jobs, audit events or logs. The composite foreign
    key ties the message to the ticket's owner, so it can never be filed under another
    customer's ticket.
    """

    __tablename__ = "customer_messages"
    __table_args__ = (
        ForeignKeyConstraint(["ticket_id", "customer_id"], ["tickets.id", "tickets.customer_id"]),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    body: Mapped[str] = mapped_column(Text)


class CustomerMemory(UUIDPrimaryKey, CreatedAt, Base):
    """One past run's verified outcome, kept as background context for later runs (FR-21).

    Structured fields only, all written by code (services/memory.py, ADR-013). There is no
    free-text column, so neither customer text nor model output can be stored here: memory
    poisoning has nowhere to go. Agents see these rows as untrusted background; eligibility,
    amounts and validation never read them.
    """

    __tablename__ = "customer_memory"
    __table_args__ = (
        # The ticket and the order must belong to the same customer, so one customer's
        # memory can never be filed under (and later shown to) another customer.
        ForeignKeyConstraint(["ticket_id", "customer_id"], ["tickets.id", "tickets.customer_id"]),
        ForeignKeyConstraint(["order_id", "customer_id"], ["orders.id", "orders.customer_id"]),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        # Serves "this customer's unexpired items" and the expiry purge.
        Index("ix_customer_memory_customer_id_expires_at", "customer_id", "expires_at"),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(index=True)
    # NULL when the run never had a verified order (it escalated before one was known).
    order_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    kind: Mapped[MemoryKind] = mapped_column(str_enum(MemoryKind, "kind"))
    outcome_code: Mapped[MemoryOutcome] = mapped_column(str_enum(MemoryOutcome, "outcome_code"))
    # The ticket's confident category; NULL if the classifier never settled on one.
    category: Mapped[TicketCategory | None] = mapped_column(str_enum(TicketCategory, "category"))
    # One item per run: a retried job finds the row already there and writes nothing.
    source_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id"), unique=True)
    expires_at: Mapped[datetime]
