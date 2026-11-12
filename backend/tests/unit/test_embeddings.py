import math
import sys

import pytest

from support_agent.core.config import Settings
from support_agent.rag.embeddings import FakeEmbedder, get_embedder


async def test_fake_embedder_is_deterministic_and_unit_length() -> None:
    embedder = FakeEmbedder()

    first, again, other = await embedder.embed_documents(["refund window", "refund window", "x"])

    assert len(first) == embedder.dimensions == 384
    assert first == again
    assert first != other
    assert math.isclose(math.sqrt(sum(v * v for v in first)), 1.0)


def test_config_selects_the_fake_embedder_without_loading_fastembed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pw@localhost:5432/db")
    monkeypatch.setenv("JWT_SECRET", "a-long-secret-" + "x" * 30)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    settings = Settings(_env_file=None)  # ignore any local .env so the test is repeatable

    assert isinstance(get_embedder(settings), FakeEmbedder)
    assert "fastembed" not in sys.modules  # the model library was never imported


def _fake_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pw@localhost:5432/db")
    monkeypatch.setenv("JWT_SECRET", "a-long-secret-" + "x" * 30)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    return Settings(_env_file=None)


def test_embedder_is_created_once_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two separate Settings objects (as two requests or tool calls would have) still
    # get the very same embedder: loading the real model happens once per process.
    first = get_embedder(_fake_settings(monkeypatch))
    second = get_embedder(_fake_settings(monkeypatch))

    assert first is second


async def test_fake_query_vector_matches_the_document_vector() -> None:
    embedder = FakeEmbedder()

    [document] = await embedder.embed_documents(["return window"])

    assert await embedder.embed_query("return window") == document
