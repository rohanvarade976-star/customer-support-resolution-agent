"""audit events use clock_timestamp

Revision ID: e2b6c9d4a1f3
Revises: d9a3b7c1e5f2
Create Date: 2026-11-14 18:00:00

audit_events.created_at defaulted to now(), the start time of the transaction. Every event a
run writes in one transaction (one saved segment) got the same time, so their order was lost
(ADR-019, Limits). clock_timestamp() is read at each insert, so new events keep the order they
were written in. Only the column's default changes: existing rows keep their times.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b6c9d4a1f3"
down_revision: str | Sequence[str] | None = "d9a3b7c1e5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("audit_events", "created_at", server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    op.alter_column("audit_events", "created_at", server_default=sa.text("now()"))
