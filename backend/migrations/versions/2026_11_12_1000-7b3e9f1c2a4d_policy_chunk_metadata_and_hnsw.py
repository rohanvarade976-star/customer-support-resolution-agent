"""policy chunk metadata and hnsw index

Revision ID: 7b3e9f1c2a4d
Revises: c4f1a7d2e9b3
Create Date: 2026-11-12 10:00:00.000000

- policy_documents gains title and refund_window_days (from each file's front matter).
- policy_chunks gains chunk_key (the stable citation id, unique), section_title and
  content_hash (lets ingestion skip unchanged chunks); one row per section per document.
- An HNSW index on policy_chunks.embedding for cosine-distance search.

The new columns are NOT NULL without a default. That is safe because nothing has
written policy rows before this milestone; the policy tables are empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b3e9f1c2a4d"
down_revision: str | Sequence[str] | None = "c4f1a7d2e9b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("policy_documents", sa.Column("title", sa.String(length=200), nullable=False))
    op.add_column("policy_documents", sa.Column("refund_window_days", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_policy_documents_refund_window_days_positive"),
        "policy_documents",
        "refund_window_days IS NULL OR refund_window_days > 0",
    )

    op.add_column("policy_chunks", sa.Column("chunk_key", sa.String(length=150), nullable=False))
    op.add_column(
        "policy_chunks", sa.Column("section_title", sa.String(length=200), nullable=False)
    )
    op.add_column("policy_chunks", sa.Column("content_hash", sa.String(length=64), nullable=False))
    op.create_unique_constraint(op.f("uq_policy_chunks_chunk_key"), "policy_chunks", ["chunk_key"])
    op.create_unique_constraint(
        op.f("uq_policy_chunks_document_id_section"), "policy_chunks", ["document_id", "section"]
    )
    # HNSW: a graph index for fast approximate nearest-neighbour search.
    # vector_cosine_ops makes it serve the cosine-distance operator (<=>).
    op.create_index(
        "ix_policy_chunks_embedding_hnsw",
        "policy_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_policy_chunks_embedding_hnsw", table_name="policy_chunks")
    op.drop_constraint(
        op.f("uq_policy_chunks_document_id_section"), "policy_chunks", type_="unique"
    )
    op.drop_constraint(op.f("uq_policy_chunks_chunk_key"), "policy_chunks", type_="unique")
    op.drop_column("policy_chunks", "content_hash")
    op.drop_column("policy_chunks", "section_title")
    op.drop_column("policy_chunks", "chunk_key")
    op.drop_constraint(
        op.f("ck_policy_documents_refund_window_days_positive"),
        "policy_documents",
        type_="check",
    )
    op.drop_column("policy_documents", "refund_window_days")
    op.drop_column("policy_documents", "title")
