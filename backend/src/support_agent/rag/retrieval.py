"""Hybrid policy retrieval: meaning search + keyword search, merged with Reciprocal Rank Fusion.

Why two searches:
- Vector search finds passages that mean the same thing in other words
  ("send it back" -> "Return window").
- Keyword search finds exact terms the model may blur ("inspection", "UPI").

Reciprocal Rank Fusion (RRF) merges two ranked lists using only positions, not raw
scores, because a cosine similarity and a text rank are not on the same scale:

    rrf_score(chunk) = sum over lists containing it of 1 / (k + rank)    (rank starts at 1)

A chunk near the top of both lists beats one that is first in only one list.

Relevance threshold: any passage whose vector similarity is below `min_similarity` is
dropped. If nothing is left, the result says "no relevant evidence" rather than returning
weak passages that an agent might over-trust.

Similarity scores only rank text. They never decide eligibility or amounts (rule 1).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.rag.embeddings import Embedder
from support_agent.repositories import policies as policy_repo
from support_agent.repositories.policies import ScoredChunk


@dataclass(frozen=True)
class RetrievalSettings:
    top_k: int
    candidate_k: int
    rrf_k: int
    min_similarity: float


@dataclass(frozen=True)
class FusedHit:
    hit: ScoredChunk  # carries the similarity and (if any) the keyword rank
    rrf_score: float


@dataclass(frozen=True)
class RetrievalResult:
    hits: list[FusedHit]  # best first, at most top_k, all above the threshold
    no_relevant_evidence: bool


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], k: int) -> list[tuple[str, float]]:
    """Merge ranked lists of ids into one list of (id, score), best first.

    Ties are broken by id, so the same input always gives the same order.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def fuse(
    vector_hits: Sequence[ScoredChunk],
    keyword_hits: Sequence[ScoredChunk],
    settings: RetrievalSettings,
) -> RetrievalResult:
    """Pure part of retrieval: fuse, apply the threshold, keep the top results."""
    by_key: dict[str, ScoredChunk] = {hit.chunk.chunk_key: hit for hit in vector_hits}
    for hit in keyword_hits:
        by_key[hit.chunk.chunk_key] = hit  # same similarity, plus the keyword rank

    fused = reciprocal_rank_fusion(
        [
            [hit.chunk.chunk_key for hit in vector_hits],
            [hit.chunk.chunk_key for hit in keyword_hits],
        ],
        k=settings.rrf_k,
    )
    relevant = [
        FusedHit(by_key[key], score)
        for key, score in fused
        if by_key[key].similarity >= settings.min_similarity
    ]
    return RetrievalResult(hits=relevant[: settings.top_k], no_relevant_evidence=not relevant)


async def hybrid_search(
    session: AsyncSession,
    embedder: Embedder,
    query: str,
    on_date: date,
    settings: RetrievalSettings,
    policy_key: str | None = None,
) -> RetrievalResult:
    """Search only the policy versions in force on `on_date`."""
    query_vector = await embedder.embed_query(query)
    vector_hits = await policy_repo.vector_search(
        session, query_vector, on_date, settings.candidate_k, policy_key
    )
    keyword_hits = await policy_repo.keyword_search(
        session, query, query_vector, on_date, settings.candidate_k, policy_key
    )
    return fuse(vector_hits, keyword_hits, settings)
