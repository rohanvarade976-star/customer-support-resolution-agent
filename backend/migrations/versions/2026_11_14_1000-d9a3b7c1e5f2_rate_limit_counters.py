"""rate limit counters

Revision ID: d9a3b7c1e5f2
Revises: c8e1f5a3d7b9
Create Date: 2026-11-14 10:00:00

One row per (hashed client key, fixed window): the API's rate limits (ADR-017). In Postgres so
the count holds across API processes. Nothing in the table is a password, email or IP address.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9a3b7c1e5f2"
down_revision: str | Sequence[str] | None = "c8e1f5a3d7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_counters",
        sa.Column("bucket_key", sa.String(length=128), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("count >= 0", name=op.f("ck_rate_limit_counters_count_non_negative")),
        sa.PrimaryKeyConstraint("bucket_key", "window_start", name=op.f("pk_rate_limit_counters")),
    )
    op.create_index(
        "ix_rate_limit_counters_expires_at", "rate_limit_counters", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_counters_expires_at", table_name="rate_limit_counters")
    op.drop_table("rate_limit_counters")
