"""Put the demo database back to its starting state (used by scripts/reset_demo.py).

Demos and workflow runs leave rows behind: tickets, runs, proposals, approvals, refund
operations, audit events, and records in the mock payment and ticketing providers. A
refund that already went through also lowers an order's refundable balance, so a demo
can no longer be repeated. This module:

1. deletes everything the seeded customers own (runs and refunds included), through
   `seed.reset`, children before parents, with no cascade deletes;
2. deletes audit events that belong to no run;
3. empties the mock providers' tables (their own Postgres schemas);
4. seeds the scenarios again from the anchor date, so every order, return and refund
   history matches data/seed/scenarios.json (and the PRD) again.

Like `seed()`, it only flushes: the caller owns the transaction. It refuses to run when
APP_ENV is "production" (`seed.reset` checks too, before anything is deleted).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import get_settings
from support_agent.db.models import AuditEvent
from support_agent.db.seed import ResetRefusedError, SeedData, reset, seed

# The mock providers' tables, children first (notes reference tickets). They live in
# their own schemas, created by the mocks on startup, so one may not exist yet.
MOCK_TABLES: tuple[str, ...] = (
    "mock_ticketing.notes",
    "mock_ticketing.tickets",
    "mock_payments.refunds",
)


@dataclass(frozen=True)
class DemoResetReport:
    deleted: dict[str, int]  # app tables, then mock tables (by "schema.table")
    created: dict[str, int]


async def _empty_mock_table(session: AsyncSession, table: str) -> int:
    exists = await session.scalar(text("SELECT to_regclass(:name)"), {"name": table})
    if exists is None:
        return 0
    result = await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608  (constant name)
    return int(result.rowcount)  # type: ignore[attr-defined]


async def reset_demo(
    session: AsyncSession, data: SeedData, now: datetime, password_hash: str
) -> DemoResetReport:
    if get_settings().app_env == "production":
        raise ResetRefusedError("Refusing to reset demo data: APP_ENV is 'production'.")

    deleted = await reset(session, data)
    orphans = await session.execute(delete(AuditEvent).where(AuditEvent.run_id.is_(None)))
    deleted[AuditEvent.__tablename__] += int(orphans.rowcount)  # type: ignore[attr-defined]
    for table in MOCK_TABLES:
        deleted[table] = await _empty_mock_table(session, table)

    created = await seed(session, data, now, password_hash)
    return DemoResetReport(deleted, created)
