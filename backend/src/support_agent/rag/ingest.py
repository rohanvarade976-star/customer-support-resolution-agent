"""Load policy files into policy_documents and policy_chunks, safely and repeatably.

Rules:
- Idempotent: a chunk whose content hash is unchanged is skipped (no re-embedding).
  Running ingestion twice in a row changes nothing the second time.
- A changed section is updated in place (its id and chunk_key stay the same).
  A section that no longer exists in the file is deleted.
- Versions never mix: each (policy_key, version) is its own document row, and chunks
  are only ever read, compared and deleted within their own document.
- One transaction: this function never commits. The caller opens a single transaction
  around it, so an error anywhere leaves the database exactly as it was.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import PolicyChunk, PolicyDocument
from support_agent.rag.chunking import Chunk, chunk_policy
from support_agent.rag.documents import PolicyFile, validate_policy_set
from support_agent.rag.embeddings import Embedder
from support_agent.repositories import policies as policy_repo


@dataclass
class IngestSummary:
    documents_created: int = 0
    documents_updated: int = 0
    documents_unchanged: int = 0
    chunks_created: int = 0
    chunks_updated: int = 0
    chunks_unchanged: int = 0
    chunks_deleted: int = 0


def content_hash(embed_text: str, model_name: str) -> str:
    """Includes the model name, so switching embedding models re-embeds every chunk."""
    return hashlib.sha256(f"{model_name}\n{embed_text}".encode()).hexdigest()


async def ingest_policies(
    session: AsyncSession, policies: Sequence[PolicyFile], embedder: Embedder
) -> IngestSummary:
    validate_policy_set(policies)  # also checked when loading; cheap to repeat here
    # Chunk every file before writing anything, so a badly structured file fails early.
    chunked = [(policy, chunk_policy(policy)) for policy in policies]

    summary = IngestSummary()
    for policy, chunks in chunked:
        document = await _upsert_document(session, policy, summary)
        await _sync_chunks(session, document, chunks, embedder, summary)
    await session.flush()  # send the writes now, so errors appear inside this call
    return summary


async def _upsert_document(
    session: AsyncSession, policy: PolicyFile, summary: IngestSummary
) -> PolicyDocument:
    meta = policy.metadata
    fields = {
        "title": meta.title,
        "effective_from": meta.effective_from,
        "effective_to": meta.effective_to,
        "refund_window_days": meta.refund_window_days,
        "investigation_threshold_days": meta.investigation_threshold_days,
        "source_uri": policy.source_uri,
    }
    document = await policy_repo.get_document(session, meta.policy_key, meta.version)
    if document is None:
        summary.documents_created += 1
        new = PolicyDocument(policy_key=meta.policy_key, version=meta.version, **fields)
        return await policy_repo.add_document(session, new)

    changed = {name: value for name, value in fields.items() if getattr(document, name) != value}
    for name, value in changed.items():
        setattr(document, name, value)
    if changed:
        summary.documents_updated += 1
    else:
        summary.documents_unchanged += 1
    return document


async def _sync_chunks(
    session: AsyncSession,
    document: PolicyDocument,
    chunks: Sequence[Chunk],
    embedder: Embedder,
    summary: IngestSummary,
) -> None:
    existing = {row.section: row for row in await policy_repo.list_chunks(session, document.id)}
    hashes = {
        chunk.section: content_hash(chunk.embed_text, embedder.model_name) for chunk in chunks
    }

    # Only new or changed chunks are embedded; that is the slow part.
    stale = [
        chunk
        for chunk in chunks
        if chunk.section not in existing
        or existing[chunk.section].content_hash != hashes[chunk.section]
    ]
    vectors = await embedder.embed_documents([chunk.embed_text for chunk in stale]) if stale else []
    if len(vectors) != len(stale) or any(len(v) != embedder.dimensions for v in vectors):
        raise ValueError("embedder returned the wrong number or size of vectors")

    summary.chunks_unchanged += len(chunks) - len(stale)
    for chunk, vector in zip(stale, vectors, strict=True):
        fields = {
            "chunk_key": chunk.chunk_key,
            "section_title": chunk.section_title,
            "content": chunk.content,
            "content_hash": hashes[chunk.section],
            "embedding": vector,
            "chunk_metadata": {
                "section_number": chunk.section_number,
                "embedding_model": embedder.model_name,
            },
        }
        row = existing.get(chunk.section)
        if row is None:
            policy_repo.add_chunk(
                session, PolicyChunk(document_id=document.id, section=chunk.section, **fields)
            )
            summary.chunks_created += 1
        else:
            for name, value in fields.items():
                setattr(row, name, value)
            summary.chunks_updated += 1

    # Sections removed from the file are removed from the database.
    wanted = {chunk.section for chunk in chunks}
    for section, row in existing.items():
        if section not in wanted:
            await policy_repo.delete_chunk(session, row)
            summary.chunks_deleted += 1
