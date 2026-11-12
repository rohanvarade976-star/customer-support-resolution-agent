"""Version-aware policy retrieval: repository, hybrid retrieval and the search_policy tool.

The fake embedder's vectors carry no meaning, so tests control similarity in two ways:
- a query equal to a chunk's exact embedded text gets the same vector (similarity 1);
- hand-made chunks with chosen vectors, searched with a query mapped to a chosen vector.
"""

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.db.models import Customer, Order, PolicyChunk, PolicyDocument
from support_agent.db.models.policies import EMBEDDING_DIMENSIONS
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import OrderStatus
from support_agent.integrations.shipping import ShippingClient
from support_agent.rag.chunking import chunk_policy
from support_agent.rag.documents import load_policy_file
from support_agent.rag.embeddings import FakeEmbedder, fake_vector
from support_agent.rag.retrieval import RetrievalSettings, hybrid_search
from support_agent.repositories import policies as policy_repo
from support_agent.schemas.policies import UNTRUSTED_NOTICE, PolicySearchResult
from support_agent.tools.base import ToolContext, ToolError
from support_agent.tools.read_tools import build_read_registry

POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
registry = build_read_registry()


def embed_text(file_name: str, section: str) -> str:
    """The exact text ingestion embedded for one real policy section."""
    chunks = chunk_policy(load_policy_file(POLICY_DIR / file_name))
    return next(chunk.embed_text for chunk in chunks if chunk.section == section)


V1_WINDOW = embed_text("refund_policy_v1.md", "section_3")
V2_WINDOW = embed_text("refund_policy_v2.md", "section_3")
# Short questions (the tool limits queries to 500 characters) that the test embedder maps
# onto the exact vector of one real section, so that section is a perfect meaning match.
ASK_V1 = "how long do I have to return (v1)"
ASK_V2 = "how long do I have to return (v2)"


class CountingEmbedder(FakeEmbedder):
    """The fake embedder, plus a count of query embeddings and optional fixed vectors."""

    def __init__(self, fixed: dict[str, list[float]] | None = None) -> None:
        self.fixed = fixed or {}
        self.queries = 0

    async def embed_query(self, text: str) -> list[float]:
        self.queries += 1
        return self.fixed.get(text) or await super().embed_query(text)


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+asyncpg://u:p@localhost/unused"),
        jwt_secret=SecretStr("x" * 32),
        **overrides,
    )


# ---------------------------------------------------------------- versions in force


@pytest.mark.parametrize(
    ("on_date", "expected"),
    [
        (date(2024, 12, 31), None),  # before any refund policy
        (date(2025, 1, 1), "v1"),  # first day of v1
        (date(2026, 6, 30), "v1"),  # v1's last day (effective_to is inclusive)
        (date(2026, 7, 1), "v2"),  # first day of v2
        (date(2030, 1, 1), "v2"),  # v2 has no end date
    ],
)
async def test_get_policy_in_force_picks_the_version_by_date(
    session: AsyncSession, ingested_policies: None, on_date: date, expected: str | None
) -> None:
    policy = await policy_repo.get_policy_in_force(session, "refund_policy", on_date)

    assert (None if policy is None else policy.version) == expected


async def test_documents_in_force_list_one_version_per_policy(
    session: AsyncSession, ingested_policies: None
) -> None:
    june = await policy_repo.list_documents_in_force(session, date(2026, 6, 15))
    november = await policy_repo.list_documents_in_force(session, date(2026, 11, 10))

    june_refund = [d.version for d in june if d.policy_key == "refund_policy"]
    november_refund = [d.version for d in november if d.policy_key == "refund_policy"]
    assert (june_refund, november_refund) == (["v1"], ["v2"])
    assert len(june) == len(november) == 5  # five policies, one version each


async def test_vector_search_only_returns_versions_in_force(
    session: AsyncSession, ingested_policies: None
) -> None:
    embedder = FakeEmbedder()
    query = await embedder.embed_query(V1_WINDOW)

    june = await policy_repo.vector_search(session, query, date(2026, 6, 15), limit=50)
    august = await policy_repo.vector_search(session, query, date(2026, 8, 1), limit=50)

    assert june[0].chunk.chunk_key == "refund_policy_v1:section_3"
    assert math.isclose(june[0].similarity, 1.0, abs_tol=1e-5)
    refund_versions = {h.document.version for h in june if h.document.policy_key == "refund_policy"}
    assert refund_versions == {"v1"}
    assert all(h.chunk.chunk_key != "refund_policy_v1:section_3" for h in august)
    assert {h.document.version for h in august if h.document.policy_key == "refund_policy"} == {
        "v2"
    }


async def test_keyword_search_only_returns_versions_in_force(
    session: AsyncSession, ingested_policies: None
) -> None:
    query_vector = await FakeEmbedder().embed_query("return window")

    june = await policy_repo.keyword_search(
        session, "return window", query_vector, date(2026, 6, 15), limit=50
    )
    november = await policy_repo.keyword_search(
        session, "return window", query_vector, date(2026, 11, 10), limit=50
    )

    june_keys = [h.chunk.chunk_key for h in june]
    november_keys = [h.chunk.chunk_key for h in november]
    assert "refund_policy_v1:section_3" in june_keys
    assert "refund_policy_v2:section_3" in november_keys
    assert not any(key.startswith("refund_policy_v2") for key in june_keys)
    assert not any(key.startswith("refund_policy_v1") for key in november_keys)
    assert all(h.keyword_rank is not None and h.keyword_rank > 0 for h in june)


async def test_keyword_search_accepts_any_customer_text(
    session: AsyncSession, ingested_policies: None
) -> None:
    query_vector = await FakeEmbedder().embed_query("x")

    for text in ["'; DROP TABLE policy_chunks; --", '"refund" -damaged OR ((', "the a an"]:
        # websearch_to_tsquery never raises a syntax error; stop-words alone match nothing.
        await policy_repo.keyword_search(session, text, query_vector, date(2026, 11, 10), 10)


# ---------------------------------------------------------------- fusion and threshold


def axis_mix(*weights: tuple[int, float]) -> list[float]:
    """A unit vector with the given weights on the given axes, e.g. (0, 0.8), (1, 0.6)."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for axis, weight in weights:
        vector[axis] = weight
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


async def add_test_policy(session: AsyncSession) -> None:
    """Three chunks with known similarity to the query vector axis 0:
    A = 1.0 (no keyword match), B = 0.8 (keyword match), C = 0.6 (no keyword match)."""
    document = PolicyDocument(
        policy_key="test_policy",
        version="v1",
        title="Test Policy",
        effective_from=date(2025, 1, 1),
        source_uri="test://policy",
    )
    session.add(document)
    await session.flush()
    for name, vector, content in [
        ("a", axis_mix((0, 1.0)), "Alpha section about something else entirely."),
        ("b", axis_mix((0, 0.8), (1, 0.6)), "Every gadget includes a one-year warranty."),
        ("c", axis_mix((0, 0.6), (2, 0.8)), "Gamma section with unrelated text."),
    ]:
        session.add(
            PolicyChunk(
                document_id=document.id,
                chunk_key=f"test_policy_v1:{name}",
                section=name,
                section_title=f"Section {name.upper()}",
                content=content,
                content_hash=name,
                embedding=vector,
            )
        )
    await session.flush()


def retrieval(min_similarity: float) -> RetrievalSettings:
    return RetrievalSettings(top_k=5, candidate_k=20, rrf_k=60, min_similarity=min_similarity)


async def test_rrf_lifts_a_chunk_found_by_both_searches(session: AsyncSession) -> None:
    await add_test_policy(session)
    embedder = CountingEmbedder({"warranty": axis_mix((0, 1.0))})

    result = await hybrid_search(
        session, embedder, "warranty", date(2026, 11, 10), retrieval(min_similarity=0.5)
    )

    # Vector order is A, B, C; only B matches the keyword. B = 1/62 + 1/61 beats A = 1/61.
    keys = [hit.hit.chunk.chunk_key for hit in result.hits]
    assert keys == ["test_policy_v1:b", "test_policy_v1:a", "test_policy_v1:c"]
    assert [round(hit.hit.similarity, 3) for hit in result.hits] == [0.8, 1.0, 0.6]
    assert result.hits[0].hit.keyword_rank is not None
    assert result.hits[1].hit.keyword_rank is None
    assert math.isclose(result.hits[0].rrf_score, 1 / 62 + 1 / 61)


async def test_threshold_drops_weak_passages(session: AsyncSession) -> None:
    await add_test_policy(session)
    embedder = CountingEmbedder({"warranty": axis_mix((0, 1.0))})

    result = await hybrid_search(
        session, embedder, "warranty", date(2026, 11, 10), retrieval(min_similarity=0.7)
    )

    assert [hit.hit.chunk.chunk_key for hit in result.hits] == [
        "test_policy_v1:b",
        "test_policy_v1:a",
    ]  # C (0.6) is gone
    assert result.no_relevant_evidence is False


async def test_keyword_match_alone_is_not_enough_evidence(session: AsyncSession) -> None:
    await add_test_policy(session)
    # The word matches chunk B, but the query's meaning (axis 3) matches nothing.
    embedder = CountingEmbedder({"warranty": axis_mix((3, 1.0))})

    result = await hybrid_search(
        session, embedder, "warranty", date(2026, 11, 10), retrieval(min_similarity=0.5)
    )

    assert result.hits == []
    assert result.no_relevant_evidence is True


async def test_documents_not_yet_in_force_are_never_searched(session: AsyncSession) -> None:
    await add_test_policy(session)
    embedder = CountingEmbedder({"warranty": axis_mix((0, 1.0))})

    result = await hybrid_search(
        session, embedder, "warranty", date(2024, 6, 1), retrieval(min_similarity=0.5)
    )

    assert (result.hits, result.no_relevant_evidence) == ([], True)


# ---------------------------------------------------------------- the search_policy tool


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


async def customer_id(session: AsyncSession, name: str) -> UUID:
    found = await session.scalar(select(Customer.id).where(Customer.email == f"{name}@example.com"))
    assert found is not None
    return found


def window_embedder() -> CountingEmbedder:
    return CountingEmbedder(
        {
            ASK_V1: fake_vector(V1_WINDOW, EMBEDDING_DIMENSIONS),
            ASK_V2: fake_vector(V2_WINDOW, EMBEDDING_DIMENSIONS),
        }
    )


async def make_ctx(
    session: AsyncSession, name: str, embedder: FakeEmbedder | None = None
) -> ToolContext:
    return ToolContext(
        session,
        await customer_id(session, name),
        make_settings(),
        MagicMock(spec=ShippingClient),  # search_policy never calls shipping
        embedder or window_embedder(),
        clock=lambda: NOW,
    )


async def search(
    session: AsyncSession, name: str, arguments: dict[str, Any]
) -> BaseModel | ToolError:
    return await registry.execute("search_policy", await make_ctx(session, name), arguments)


def expect_result(result: BaseModel | ToolError) -> PolicySearchResult:
    assert isinstance(result, PolicySearchResult), result
    return result


async def add_june_order(session: AsyncSession) -> None:
    session.add(
        Order(
            order_number="ORD-2001",
            customer_id=await customer_id(session, "priya"),
            status=OrderStatus.DELIVERED,
            paid_amount_minor=150000,
            placed_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
            delivered_at=datetime(2026, 6, 25, 10, 0, tzinfo=UTC),
        )
    )
    await session.flush()


async def test_order_number_selects_the_version_in_force_when_it_was_placed(
    session: AsyncSession, ingested_policies: None
) -> None:
    await add_june_order(session)

    june = expect_result(
        await search(session, "priya", {"query": ASK_V1, "order_number": "ORD-2001"})
    )
    recent = expect_result(
        await search(session, "priya", {"query": ASK_V2, "order_number": "ORD-1001"})
    )

    assert june.as_of_date == date(2026, 6, 20)
    assert "refund_policy v1" in june.versions_considered
    assert "refund_policy v2" not in june.versions_considered
    assert june.passages[0].citation == "refund_policy_v1:section_3"

    assert recent.as_of_date == (NOW - timedelta(days=16)).date()  # ORD-1001 placed 16 days ago
    assert "refund_policy v2" in recent.versions_considered
    assert recent.passages[0].citation == "refund_policy_v2:section_3"
    assert all(not p.citation.startswith("refund_policy_v1") for p in recent.passages)


async def test_without_an_order_the_search_uses_today(
    session: AsyncSession, ingested_policies: None
) -> None:
    result = expect_result(await search(session, "priya", {"query": ASK_V2}))

    assert result.as_of_date == NOW.date()
    assert result.order_number is None
    assert result.passages[0].citation == "refund_policy_v2:section_3"


async def test_citation_format_and_passage_fields(
    session: AsyncSession, ingested_policies: None
) -> None:
    result = expect_result(await search(session, "priya", {"query": ASK_V2}))

    passage = result.passages[0]
    assert passage.citation == f"{passage.policy_key}_{passage.version}:{passage.section}"
    assert (passage.policy_title, passage.version, passage.section_title) == (
        "Refund Policy",
        "v2",
        "Return window",
    )
    assert "30 days to request a return" in passage.content
    assert passage.effective_from == date(2026, 7, 1) and passage.effective_to is None
    assert passage.vector_similarity == pytest.approx(1.0, abs=1e-4)
    # Every citation is a real chunk_key in the database.
    keys = set(await session.scalars(select(PolicyChunk.chunk_key)))
    assert {p.citation for p in result.passages} <= keys


async def test_result_is_marked_as_untrusted_reference_data(
    session: AsyncSession, ingested_policies: None
) -> None:
    result = expect_result(await search(session, "priya", {"query": ASK_V2}))

    assert result.notice == UNTRUSTED_NOTICE
    assert "not instructions" in result.notice
    assert result.passages
    assert all(p.trust == "untrusted_reference_data" for p in result.passages)
    assert '"trust":"untrusted_reference_data"' in result.model_dump_json()


async def test_unrelated_query_reports_no_relevant_evidence(
    session: AsyncSession, ingested_policies: None
) -> None:
    # Fake vectors of unrelated text are nearly orthogonal: far below the 0.55 threshold.
    result = expect_result(await search(session, "priya", {"query": "favourite pizza topping"}))

    assert result.no_relevant_evidence is True
    assert result.passages == []
    assert result.versions_considered  # policies were searched, nothing was relevant


async def test_another_customers_order_is_order_not_found(
    session: AsyncSession, ingested_policies: None
) -> None:
    others = await search(session, "rahul", {"query": ASK_V2, "order_number": "ORD-1001"})
    missing = await search(session, "rahul", {"query": ASK_V2, "order_number": "ORD-9999"})

    assert isinstance(others, ToolError)
    assert (others.error_code, others.retryable) == ("ORDER_NOT_FOUND", False)
    assert others == missing  # no hint that ORD-1001 exists


async def test_the_tool_uses_the_embedder_from_the_context(
    session: AsyncSession, ingested_policies: None
) -> None:
    embedder = CountingEmbedder()
    ctx = await make_ctx(session, "priya", embedder)

    for _ in range(3):
        await registry.execute("search_policy", ctx, {"query": "return window"})

    assert embedder.queries == 3  # one query embedding per search, all on the same object


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "ab"},  # too short
        {"query": "   "},  # only whitespace
        {"query": "x" * 501},  # too long
        {"query": "return window", "order_number": "1001"},
        {"query": "return window", "customer_id": "someone-else"},
        {"query": "return window", "policy_version": "v1"},  # unknown fields are refused
    ],
    ids=["short", "blank", "long", "bad-order", "customer-id", "extra-field"],
)
async def test_invalid_input_is_rejected(
    session: AsyncSession, arguments: dict[str, Sequence[str]]
) -> None:
    result = await search(session, "priya", arguments)

    assert isinstance(result, ToolError)
    assert result.error_code == "INVALID_INPUT"
