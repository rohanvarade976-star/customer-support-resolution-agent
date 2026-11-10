"""Database storage for the mock ticketing provider.

Tickets and notes live in their own Postgres schema, "mock_ticketing", separate from our
app's tables. The tables use their own MetaData, so Alembic never sees them; the mock
creates them itself on startup.

Both creates follow the same idempotency pattern as the payments mock: a UNIQUE
idempotency_key column plus INSERT ... ON CONFLICT DO NOTHING, so two concurrent
requests with the same key can never both insert.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    RowMapping,
    String,
    Table,
    Text,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from mock_providers.ticketing.schemas import NoteCreate, TicketCreate, TicketStatus

SCHEMA = "mock_ticketing"

metadata = MetaData(schema=SCHEMA)

tickets = Table(
    "tickets",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("idempotency_key", String(255), nullable=False, unique=True),
    Column("customer_reference", String(100), nullable=False),
    Column("order_reference", String(100), nullable=True),
    Column("subject", String(200), nullable=False),
    Column("description", Text, nullable=False),
    Column("status", String(20), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "status IN ('open', 'pending', 'resolved', 'closed')", name="tickets_status_valid"
    ),
)

notes = Table(
    "notes",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("ticket_id", String(40), ForeignKey("tickets.id"), nullable=False, index=True),
    # Unique across all notes, not per ticket: reusing a key on another ticket is a 409.
    Column("idempotency_key", String(255), nullable=False, unique=True),
    Column("body", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class StoredTicket:
    id: str
    idempotency_key: str
    customer_reference: str
    order_reference: str | None
    subject: str
    description: str
    status: TicketStatus
    created_at: datetime
    updated_at: datetime

    def same_payload_as(self, request: TicketCreate) -> bool:
        return (
            self.customer_reference == request.customer_reference
            and self.order_reference == request.order_reference
            and self.subject == request.subject
            and self.description == request.description
        )


@dataclass(frozen=True)
class StoredNote:
    id: str
    ticket_id: str
    idempotency_key: str
    body: str
    created_at: datetime

    def same_payload_as(self, ticket_id: str, request: NoteCreate) -> bool:
        return self.ticket_id == ticket_id and self.body == request.body


async def create_schema(engine: AsyncEngine) -> None:
    """Create the schema and tables if they do not exist yet (safe to run every startup)."""
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        await conn.run_sync(metadata.create_all)


# ---------------------------------------------------------------- tickets


async def insert_ticket(
    engine: AsyncEngine,
    *,
    ticket_id: str,
    idempotency_key: str,
    request: TicketCreate,
    created_at: datetime,
) -> StoredTicket | None:
    """Insert a new open ticket. Returns None if the idempotency key already exists."""
    statement = (
        insert(tickets)
        .values(
            id=ticket_id,
            idempotency_key=idempotency_key,
            status=TicketStatus.OPEN.value,
            created_at=created_at,
            updated_at=created_at,
            **request.model_dump(),
        )
        .on_conflict_do_nothing(index_elements=[tickets.c.idempotency_key])
        .returning(*tickets.c)
    )
    async with engine.begin() as conn:
        row = (await conn.execute(statement)).mappings().first()
    return _to_ticket(row) if row else None


async def get_ticket(engine: AsyncEngine, ticket_id: str) -> StoredTicket | None:
    statement = select(tickets).where(tickets.c.id == ticket_id)
    async with engine.connect() as conn:
        row = (await conn.execute(statement)).mappings().first()
    return _to_ticket(row) if row else None


async def get_ticket_by_idempotency_key(
    engine: AsyncEngine, idempotency_key: str
) -> StoredTicket | None:
    statement = select(tickets).where(tickets.c.idempotency_key == idempotency_key)
    async with engine.connect() as conn:
        row = (await conn.execute(statement)).mappings().first()
    return _to_ticket(row) if row else None


async def update_ticket_status(
    engine: AsyncEngine, ticket_id: str, status: TicketStatus, updated_at: datetime
) -> StoredTicket | None:
    """Set the status. Returns None if the ticket does not exist."""
    statement = (
        update(tickets)
        .where(tickets.c.id == ticket_id)
        .values(status=status.value, updated_at=updated_at)
        .returning(*tickets.c)
    )
    async with engine.begin() as conn:
        row = (await conn.execute(statement)).mappings().first()
    return _to_ticket(row) if row else None


# ---------------------------------------------------------------- notes


async def insert_note(
    engine: AsyncEngine,
    *,
    note_id: str,
    ticket_id: str,
    idempotency_key: str,
    request: NoteCreate,
    created_at: datetime,
) -> StoredNote | None:
    """Insert a new note. Returns None if the idempotency key already exists."""
    statement = (
        insert(notes)
        .values(
            id=note_id,
            ticket_id=ticket_id,
            idempotency_key=idempotency_key,
            body=request.body,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=[notes.c.idempotency_key])
        .returning(*notes.c)
    )
    async with engine.begin() as conn:
        row = (await conn.execute(statement)).mappings().first()
    return StoredNote(**row) if row else None


async def get_note_by_idempotency_key(
    engine: AsyncEngine, idempotency_key: str
) -> StoredNote | None:
    statement = select(notes).where(notes.c.idempotency_key == idempotency_key)
    async with engine.connect() as conn:
        row = (await conn.execute(statement)).mappings().first()
    return StoredNote(**row) if row else None


async def list_notes(engine: AsyncEngine, ticket_id: str) -> list[StoredNote]:
    statement = (
        select(notes).where(notes.c.ticket_id == ticket_id).order_by(notes.c.created_at, notes.c.id)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(statement)).mappings().all()
    return [StoredNote(**row) for row in rows]


def _to_ticket(row: RowMapping) -> StoredTicket:
    data = dict(row)
    data["status"] = TicketStatus(data["status"])
    return StoredTicket(**data)
