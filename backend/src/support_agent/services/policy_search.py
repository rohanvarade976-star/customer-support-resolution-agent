"""Policy search for one customer: pick the right date, then run hybrid retrieval.

With an order number, the date is the order's placed_at date, and the order is looked up
scoped to the customer: another customer's order is ORDER_NOT_FOUND, exactly like a
missing one (FR-2). Without an order number, the date is today.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.domain.policies import policy_date
from support_agent.rag.embeddings import Embedder
from support_agent.rag.retrieval import FusedHit, RetrievalSettings, hybrid_search
from support_agent.repositories import orders
from support_agent.repositories import policies as policy_repo
from support_agent.schemas.policies import PolicyPassage, PolicySearchResult
from support_agent.services.orders import order_not_found


def retrieval_settings(settings: Settings) -> RetrievalSettings:
    return RetrievalSettings(
        top_k=settings.policy_search_top_k,
        candidate_k=settings.policy_search_candidate_k,
        rrf_k=settings.policy_rrf_k,
        min_similarity=settings.policy_min_similarity,
    )


def _to_passage(fused: FusedHit) -> PolicyPassage:
    chunk, document = fused.hit.chunk, fused.hit.document
    return PolicyPassage(
        citation=chunk.chunk_key,
        policy_key=document.policy_key,
        policy_title=document.title,
        version=document.version,
        section=chunk.section,
        section_title=chunk.section_title,
        effective_from=document.effective_from,
        effective_to=document.effective_to,
        content=chunk.content,
        vector_similarity=round(fused.hit.similarity, 4),
        keyword_rank=None if fused.hit.keyword_rank is None else round(fused.hit.keyword_rank, 4),
        rrf_score=round(fused.rrf_score, 6),
    )


async def search_policy(
    session: AsyncSession,
    embedder: Embedder,
    settings: Settings,
    customer_id: UUID,
    query: str,
    order_number: str | None,
    now: datetime,
) -> PolicySearchResult:
    if order_number is None:
        as_of = policy_date(now)
    else:
        order = await orders.get_for_customer(session, customer_id, order_number)
        if order is None:
            raise order_not_found()
        as_of = policy_date(order.placed_at)

    documents = await policy_repo.list_documents_in_force(session, as_of)
    result = await hybrid_search(session, embedder, query, as_of, retrieval_settings(settings))
    return PolicySearchResult(
        as_of_date=as_of,
        order_number=order_number,
        versions_considered=[f"{doc.policy_key} {doc.version}" for doc in documents],
        no_relevant_evidence=result.no_relevant_evidence,
        passages=[_to_passage(hit) for hit in result.hits],
    )
