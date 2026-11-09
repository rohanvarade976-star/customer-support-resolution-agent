"""Versioned policy documents and their embedded chunks for RAG."""

import uuid
from datetime import date
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UUIDPrimaryKey

# Output size of the fastembed model we plan to use (BAAI/bge-small-en-v1.5).
EMBEDDING_DIMENSIONS = 384


class PolicyDocument(UUIDPrimaryKey, CreatedAt, Base):
    __tablename__ = "policy_documents"
    __table_args__ = (
        UniqueConstraint("policy_key", "version"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="valid_effective_range"
        ),
    )

    policy_key: Mapped[str] = mapped_column(String(64))  # e.g. "refund_policy"
    version: Mapped[str] = mapped_column(String(32))  # e.g. "v2"
    effective_from: Mapped[date]
    effective_to: Mapped[date | None]  # NULL = still in force
    source_uri: Mapped[str] = mapped_column(String(500))


class PolicyChunk(UUIDPrimaryKey, CreatedAt, Base):
    __tablename__ = "policy_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_documents.id"), index=True)
    section: Mapped[str] = mapped_column(String(100))  # cited as e.g. "section_3"
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    # `metadata` is reserved on SQLAlchemy models, so the Python name differs from the column.
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", default=dict, server_default="{}"
    )
