"""Reciprocal Rank Fusion and the relevance threshold. Pure functions, no database."""

import math
from datetime import date

import pytest

from support_agent.db.models import PolicyChunk, PolicyDocument
from support_agent.rag.retrieval import RetrievalSettings, fuse, reciprocal_rank_fusion
from support_agent.repositories.policies import ScoredChunk

DOC = PolicyDocument(
    policy_key="refund_policy", version="v2", title="Refund Policy", effective_from=date(2026, 7, 1)
)


def hit(key: str, similarity: float, keyword_rank: float | None = None) -> ScoredChunk:
    chunk = PolicyChunk(chunk_key=key, section=key, section_title=key, content=key)
    return ScoredChunk(chunk, DOC, similarity, keyword_rank)


def settings(**overrides: float) -> RetrievalSettings:
    values: dict[str, float] = {"top_k": 5, "candidate_k": 20, "rrf_k": 60, "min_similarity": 0.5}
    values.update(overrides)
    return RetrievalSettings(
        top_k=int(values["top_k"]),
        candidate_k=int(values["candidate_k"]),
        rrf_k=int(values["rrf_k"]),
        min_similarity=values["min_similarity"],
    )


def test_rrf_scores_are_one_over_k_plus_rank() -> None:
    fused = dict(reciprocal_rank_fusion([["a", "b"], ["b"]], k=60))

    assert math.isclose(fused["a"], 1 / 61)
    assert math.isclose(fused["b"], 1 / 62 + 1 / 61)


def test_high_in_both_lists_beats_first_in_only_one() -> None:
    vector = ["only_vector", "both", "x"]
    keyword = ["only_keyword", "both", "y"]

    order = [key for key, _ in reciprocal_rank_fusion([vector, keyword], k=60)]

    # "both": 2/62 = 0.0323; "only_vector" / "only_keyword": 1/61 = 0.0164.
    assert order[0] == "both"


def test_ties_are_broken_by_id_so_order_is_stable() -> None:
    order = [key for key, _ in reciprocal_rank_fusion([["b"], ["a"]], k=60)]

    assert order == ["a", "b"]  # equal scores (1/61 each): alphabetical


def test_fuse_keeps_the_keyword_rank_and_orders_by_rrf() -> None:
    vector_hits = [hit("v:1", 0.9), hit("both:1", 0.8)]
    keyword_hits = [hit("both:1", 0.8, keyword_rank=0.4), hit("k:1", 0.7, keyword_rank=0.2)]

    result = fuse(vector_hits, keyword_hits, settings())

    # both:1 = 1/62 + 1/61; v:1 = 1/61 (rank 1 in one list); k:1 = 1/62 (rank 2 in one list).
    assert [h.hit.chunk.chunk_key for h in result.hits] == ["both:1", "v:1", "k:1"]
    assert result.hits[0].hit.keyword_rank == 0.4
    assert result.no_relevant_evidence is False


def test_passages_below_the_threshold_are_dropped() -> None:
    result = fuse([hit("good", 0.7), hit("weak", 0.3)], [hit("weak", 0.3, 0.9)], settings())

    assert [h.hit.chunk.chunk_key for h in result.hits] == ["good"]


@pytest.mark.parametrize("similarity", [0.49, 0.0, -0.2])
def test_nothing_above_the_threshold_means_no_relevant_evidence(similarity: float) -> None:
    result = fuse([hit("a", similarity)], [hit("a", similarity, 0.5)], settings())

    assert result.no_relevant_evidence is True
    assert result.hits == []


def test_no_hits_at_all_means_no_relevant_evidence() -> None:
    result = fuse([], [], settings())

    assert (result.hits, result.no_relevant_evidence) == ([], True)


def test_top_k_limits_the_results() -> None:
    hits = [hit(f"c{i}", 0.9 - i / 100) for i in range(8)]

    result = fuse(hits, [], settings(top_k=3))

    assert [h.hit.chunk.chunk_key for h in result.hits] == ["c0", "c1", "c2"]
