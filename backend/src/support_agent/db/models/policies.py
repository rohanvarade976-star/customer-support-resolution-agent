"""Versioned policy documents and their embedded chunks for RAG."""

import uuid
from datetime import date
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UUIDPrimaryKey

# Output size of the fastembed model we use (BAAI/bge-small-en-v1.5).
EMBEDDING_DIMENSIONS = 384

# Must match the generated column in the full-text migration exactly.
SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english'::regconfig, coalesce(section_title, '')), 'A') || "
    "setweight(to_tsvector('english'::regconfig, coalesce(content, '')), 'B')"
)


class PolicyDocument(UUIDPrimaryKey, CreatedAt, Base):
    __tablename__ = "policy_documents"
    __table_args__ = (
        UniqueConstraint("policy_key", "version"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="valid_effective_range"
        ),
        CheckConstraint(
            "refund_window_days IS NULL OR refund_window_days > 0",
            name="refund_window_days_positive",
        ),
    )

    policy_key: Mapped[str] = mapped_column(String(64))  # e.g. "refund_policy"
    version: Mapped[str] = mapped_column(String(32))  # e.g. "v2"
    title: Mapped[str] = mapped_column(String(200))  # e.g. "Refund Policy"
    effective_from: Mapped[date]
    effective_to: Mapped[date | None]  # last day in force (inclusive); NULL = still in force
    refund_window_days: Mapped[int | None]  # set only for refund_policy
    source_uri: Mapped[str] = mapped_column(String(500))


class PolicyChunk(UUIDPrimaryKey, CreatedAt, Base):
    __tablename__ = "policy_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "section"),
        # Approximate nearest-neighbour index for cosine-distance search (pgvector HNSW).
        Index(
            "ix_policy_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # Inverted index over the words in search_vector, for keyword search.
        Index("ix_policy_chunks_search_vector_gin", "search_vector", postgresql_using="gin"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_documents.id"), index=True)
    chunk_key: Mapped[str] = mapped_column(String(150), unique=True)  # "refund_policy_v2:section_3"
    section: Mapped[str] = mapped_column(String(100))  # cited as e.g. "section_3"
    section_title: Mapped[str] = mapped_column(String(200))  # e.g. "Return window"
    content: Mapped[str] = mapped_column(Text)
    # SHA-256 of the embedding model name + embedded text. Same hash = nothing to redo.
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    # `metadata` is reserved on SQLAlchemy models, so the Python name differs from the column.
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", default=dict, server_default="{}"
    )
    # Words of the title (weight A) and content (weight B), computed by Postgres itself.
    # Never written by Python; it updates automatically when the text changes.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, Computed(SEARCH_VECTOR_SQL, persisted=True), deferred=True
    )
