"""All SQL for policy documents and policy chunks.

Every search here is limited to the document versions "in force" on one date:
effective_from <= date and (effective_to is NULL or date <= effective_to).
effective_to is the last day a version applies, so both ends are inclusive.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, func, literal, or_, select
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import PolicyChunk, PolicyDocument


@dataclass(frozen=True)
class ScoredChunk:
    """One search hit: the chunk, the document version it belongs to, and its scores."""

    chunk: PolicyChunk
    document: PolicyDocument
    similarity: float  # cosine similarity to the query: 1 = same direction, 0 = unrelated
    keyword_rank: float | None  # full-text rank; None when found by vector search only


async def get_document(
    session: AsyncSession, policy_key: str, version: str
) -> PolicyDocument | None:
    stmt = select(PolicyDocument).where(
        PolicyDocument.policy_key == policy_key, PolicyDocument.version == version
    )
    return await session.scalar(stmt)


async def add_document(session: AsyncSession, document: PolicyDocument) -> PolicyDocument:
    session.add(document)
    await session.flush()  # assigns nothing new (id is set in Python) but surfaces errors early
    return document


async def list_chunks(session: AsyncSession, document_id: UUID) -> list[PolicyChunk]:
    """Every chunk of one document version. Never returns chunks of another version."""
    stmt = select(PolicyChunk).where(PolicyChunk.document_id == document_id)
    return list(await session.scalars(stmt))


def add_chunk(session: AsyncSession, chunk: PolicyChunk) -> None:
    session.add(chunk)


async def delete_chunk(session: AsyncSession, chunk: PolicyChunk) -> None:
    await session.delete(chunk)


# ---------------------------------------------------------------- versions in force


def _in_force(on_date: date, policy_key: str | None) -> list[ColumnElement[bool]]:
    conditions = [
        PolicyDocument.effective_from <= on_date,
        or_(PolicyDocument.effective_to.is_(None), PolicyDocument.effective_to >= on_date),
    ]
    if policy_key is not None:
        conditions.append(PolicyDocument.policy_key == policy_key)
    return conditions


async def get_policy_in_force(
    session: AsyncSession, policy_key: str, on_date: date
) -> PolicyDocument | None:
    """The one version of a policy in force on the date, or None if no version applies.

    Ingestion rejects overlapping versions, so two matches mean the database is
    inconsistent; `one_or_none` then raises instead of silently picking one.
    """
    stmt = select(PolicyDocument).where(*_in_force(on_date, policy_key))
    return (await session.scalars(stmt)).one_or_none()


async def list_documents_in_force(
    session: AsyncSession, on_date: date, policy_key: str | None = None
) -> list[PolicyDocument]:
    stmt = (
        select(PolicyDocument)
        .where(*_in_force(on_date, policy_key))
        .order_by(PolicyDocument.policy_key, PolicyDocument.version)
    )
    return list(await session.scalars(stmt))


# ---------------------------------------------------------------- search


async def vector_search(
    session: AsyncSession,
    query_vector: Sequence[float],
    on_date: date,
    limit: int,
    policy_key: str | None = None,
) -> list[ScoredChunk]:
    """The `limit` chunks closest in meaning to the query (smallest cosine distance)."""
    distance = PolicyChunk.embedding.cosine_distance(query_vector)
    stmt = (
        select(PolicyChunk, PolicyDocument, distance.label("distance"))
        .join(PolicyDocument, PolicyDocument.id == PolicyChunk.document_id)
        .where(*_in_force(on_date, policy_key))
        .order_by(distance, PolicyChunk.chunk_key)  # chunk_key breaks ties, so order is stable
        .limit(limit)
    )
    rows = await session.execute(stmt)
    return [
        ScoredChunk(chunk, document, similarity=1.0 - float(dist), keyword_rank=None)
        for chunk, document, dist in rows
    ]


async def keyword_search(
    session: AsyncSession,
    query_text: str,
    query_vector: Sequence[float],
    on_date: date,
    limit: int,
    policy_key: str | None = None,
) -> list[ScoredChunk]:
    """The `limit` chunks that best match the query's words (Postgres full-text search).

    websearch_to_tsquery accepts any text (quotes, dashes, "or") without syntax errors,
    so raw customer wording is safe to pass. The query vector is used only to report
    each hit's similarity, so every hit can be held to the same relevance threshold.
    """
    tsquery: Any = func.websearch_to_tsquery(literal("english", type_=REGCONFIG), query_text)
    rank = func.ts_rank_cd(PolicyChunk.search_vector, tsquery)
    distance = PolicyChunk.embedding.cosine_distance(query_vector)
    stmt = (
        select(PolicyChunk, PolicyDocument, rank.label("rank"), distance.label("distance"))
        .join(PolicyDocument, PolicyDocument.id == PolicyChunk.document_id)
        .where(*_in_force(on_date, policy_key), PolicyChunk.search_vector.bool_op("@@")(tsquery))
        .order_by(rank.desc(), PolicyChunk.chunk_key)
        .limit(limit)
    )
    rows = await session.execute(stmt)
    return [
        ScoredChunk(chunk, document, similarity=1.0 - float(dist), keyword_rank=float(score))
        for chunk, document, score, dist in rows
    ]


async def get_chunks_by_keys(
    session: AsyncSession, chunk_keys: Sequence[str]
) -> list[tuple[PolicyChunk, PolicyDocument]]:
    """The chunks with these exact keys and their documents. Unknown keys are left out."""
    if not chunk_keys:
        return []
    stmt = (
        select(PolicyChunk, PolicyDocument)
        .join(PolicyDocument, PolicyDocument.id == PolicyChunk.document_id)
        .where(PolicyChunk.chunk_key.in_(list(chunk_keys)))
    )
    rows = (await session.execute(stmt)).all()
    by_key = {chunk.chunk_key: (chunk, document) for chunk, document in rows}
    return [by_key[key] for key in chunk_keys if key in by_key]  # in the caller's order
