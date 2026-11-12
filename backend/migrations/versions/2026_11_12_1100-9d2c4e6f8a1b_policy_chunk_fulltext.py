"""policy chunk full-text search column

Revision ID: 9d2c4e6f8a1b
Revises: 7b3e9f1c2a4d
Create Date: 2026-11-12 11:00:00.000000

- policy_chunks gains search_vector: a tsvector that Postgres computes and stores itself
  from section_title (weight A, counts more) and content (weight B). Because it is a
  GENERATED column, it can never be out of date and ingestion does not need to know it.
- A GIN index on it, so keyword search does not scan every row.

The 'english' config is written out explicitly: a generated column needs an expression
that always gives the same result, and the server's default config could change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9d2c4e6f8a1b"
down_revision: str | Sequence[str] | None = "7b3e9f1c2a4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english'::regconfig, coalesce(section_title, '')), 'A') || "
    "setweight(to_tsvector('english'::regconfig, coalesce(content, '')), 'B')"
)


def upgrade() -> None:
    op.add_column(
        "policy_chunks",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR_SQL, persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_policy_chunks_search_vector_gin",
        "policy_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_policy_chunks_search_vector_gin", table_name="policy_chunks")
    op.drop_column("policy_chunks", "search_vector")
