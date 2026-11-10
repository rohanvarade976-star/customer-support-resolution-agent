"""Database storage for the mock payment provider.

Refunds live in their own Postgres schema, "mock_payments", next to (but separate from)
our app's tables. The table uses its own MetaData, so Alembic never sees or manages it;
the mock creates it itself on startup.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    MetaData,
    RowMapping,
    String,
    Table,
    Text,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from mock_providers.payments.schemas import FinalOutcome, RefundRequest

SCHEMA = "mock_payments"

metadata = MetaData(schema=SCHEMA)

refunds = Table(
    "refunds",
    metadata,
    Column("provider_reference", String(40), primary_key=True),
    # unique=True is what makes idempotency safe under concurrency (see insert_refund).
    Column("idempotency_key", String(255), nullable=False, unique=True),
    Column("order_reference", String(100), nullable=False),
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("reason", Text, nullable=False),
    Column("final_outcome", String(20), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("amount_minor > 0", name="refunds_amount_positive"),
    CheckConstraint("final_outcome IN ('succeeded', 'failed')", name="refunds_final_outcome"),
)


@dataclass(frozen=True)
class StoredRefund:
    provider_reference: str
    idempotency_key: str
    order_reference: str
    amount_minor: int
    currency: str
    reason: str
    final_outcome: FinalOutcome
    created_at: datetime

    def same_payload_as(self, request: RefundRequest) -> bool:
        return (
            self.order_reference == request.order_reference
            and self.amount_minor == request.amount_minor
            and self.currency == request.currency
            and self.reason == request.reason
        )


async def create_schema(engine: AsyncEngine) -> None:
    """Create the schema and table if they do not exist yet (safe to run every startup)."""
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        await conn.run_sync(metadata.create_all)


async def insert_refund(
    engine: AsyncEngine,
    *,
    provider_reference: str,
    idempotency_key: str,
    request: RefundRequest,
    final_outcome: FinalOutcome,
    created_at: datetime,
) -> StoredRefund | None:
    """Insert a new refund. Returns None if the idempotency key already exists.

    One INSERT ... ON CONFLICT DO NOTHING statement, not "check then insert": if two
    requests with the same key arrive together, Postgres lets exactly one insert win.
    """
    statement = (
        insert(refunds)
        .values(
            provider_reference=provider_reference,
            idempotency_key=idempotency_key,
            final_outcome=final_outcome.value,
            created_at=created_at,
            **request.model_dump(),
        )
        .on_conflict_do_nothing(index_elements=[refunds.c.idempotency_key])
        .returning(*refunds.c)
    )
    async with engine.begin() as conn:
        row = (await conn.execute(statement)).mappings().first()
    return _to_refund(row) if row else None


async def get_by_reference(engine: AsyncEngine, provider_reference: str) -> StoredRefund | None:
    statement = select(refunds).where(refunds.c.provider_reference == provider_reference)
    async with engine.connect() as conn:
        row = (await conn.execute(statement)).mappings().first()
    return _to_refund(row) if row else None


async def get_by_idempotency_key(engine: AsyncEngine, idempotency_key: str) -> StoredRefund | None:
    statement = select(refunds).where(refunds.c.idempotency_key == idempotency_key)
    async with engine.connect() as conn:
        row = (await conn.execute(statement)).mappings().first()
    return _to_refund(row) if row else None


def _to_refund(row: RowMapping) -> StoredRefund:
    data = dict(row)
    data["final_outcome"] = FinalOutcome(data["final_outcome"])
    return StoredRefund(**data)
