"""Policy ingestion against a real database, using the fake embedder (no model download).

Every check reads database rows, not the summary the function returns.
"""

import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import PolicyChunk, PolicyDocument
from support_agent.rag.documents import PolicyFile, load_policy_directory
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.rag.ingest import ingest_policies

POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"


class CountingEmbedder(FakeEmbedder):
    """The fake embedder, plus a record of every text it was asked to embed."""

    def __init__(self, model_name: str = "fake-sha256") -> None:
        self.model_name = model_name
        self.embedded: list[str] = []

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return await super().embed_documents(texts)


class BrokenEmbedder(FakeEmbedder):
    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("model crashed")


@pytest.fixture
def policy_dir(tmp_path: Path) -> Path:
    """A private copy of the real policies that a test may edit."""
    target = tmp_path / "policies"
    shutil.copytree(POLICY_DIR, target)
    return target


async def chunk_rows(session: AsyncSession) -> dict[str, tuple[object, ...]]:
    """chunk_key -> (id, document key, content hash, embedding) for every chunk."""
    rows = await session.execute(
        select(PolicyChunk, PolicyDocument.policy_key, PolicyDocument.version).join(
            PolicyDocument, PolicyDocument.id == PolicyChunk.document_id
        )
    )
    return {
        chunk.chunk_key: (chunk.id, f"{key}_{version}", chunk.content_hash, list(chunk.embedding))
        for chunk, key, version in rows
    }


def edit(policy_dir: Path, name: str, old: str, new: str) -> list[PolicyFile]:
    path = policy_dir / name
    content = path.read_text(encoding="utf-8")
    assert old in content
    path.write_text(content.replace(old, new), encoding="utf-8")
    return load_policy_directory(policy_dir)


async def test_first_run_creates_every_document_and_chunk(
    session: AsyncSession, policy_dir: Path
) -> None:
    await ingest_policies(session, load_policy_directory(policy_dir), FakeEmbedder())

    documents = (await session.scalars(select(PolicyDocument))).all()
    assert len(documents) == 6
    v2 = next(d for d in documents if d.policy_key == "refund_policy" and d.version == "v2")
    assert v2.title == "Refund Policy"
    assert v2.refund_window_days == 30
    assert v2.effective_to is None

    rows = await chunk_rows(session)
    section_3 = await session.scalar(
        select(PolicyChunk).where(PolicyChunk.chunk_key == "refund_policy_v2:section_3")
    )
    assert section_3 is not None
    assert section_3.section == "section_3"
    assert section_3.section_title == "Return window"
    assert section_3.document_id == v2.id
    # Versions never mix: every chunk sits under the document its id names.
    assert all(key.startswith(f"{document_key}:") for key, (_, document_key, _, _) in rows.items())


async def test_second_run_changes_nothing(session: AsyncSession, policy_dir: Path) -> None:
    policies = load_policy_directory(policy_dir)
    await ingest_policies(session, policies, FakeEmbedder())
    before = await chunk_rows(session)

    embedder = CountingEmbedder()
    await ingest_policies(session, policies, embedder)

    assert embedder.embedded == []  # nothing was re-embedded
    assert await chunk_rows(session) == before  # same ids, hashes and vectors


async def test_changed_text_updates_only_that_chunk(
    session: AsyncSession, policy_dir: Path
) -> None:
    await ingest_policies(session, load_policy_directory(policy_dir), FakeEmbedder())
    before = await chunk_rows(session)

    policies = edit(
        policy_dir,
        "refund_policy_v2.md",
        "usually reaches the customer within\n5 to 7",
        "usually reaches the customer within\n3 to 5",
    )
    embedder = CountingEmbedder()
    await ingest_policies(session, policies, embedder)
    after = await chunk_rows(session)

    changed = {key for key in before if before[key] != after[key]}
    assert changed == {"refund_policy_v2:section_7"}
    assert len(embedder.embedded) == 1
    assert (
        after["refund_policy_v2:section_7"][0] == before["refund_policy_v2:section_7"][0]
    )  # same id
    # v1 has the same sentence; it belongs to another version and is untouched.
    assert after["refund_policy_v1:section_7"] == before["refund_policy_v1:section_7"]


async def test_removed_section_is_deleted(session: AsyncSession, policy_dir: Path) -> None:
    await ingest_policies(session, load_policy_directory(policy_dir), FakeEmbedder())

    path = policy_dir / "shipping_delays_v1.md"
    content = path.read_text(encoding="utf-8")
    path.write_text(content[: content.index("## 6. Ticket updates")], encoding="utf-8")
    await ingest_policies(session, load_policy_directory(policy_dir), FakeEmbedder())

    keys = set(await chunk_rows(session))
    assert "shipping_delays_v1:section_5" in keys
    assert "shipping_delays_v1:section_6" not in keys


async def test_changing_the_embedding_model_re_embeds_everything(
    session: AsyncSession, policy_dir: Path
) -> None:
    policies = load_policy_directory(policy_dir)
    await ingest_policies(session, policies, FakeEmbedder())
    total = len(await chunk_rows(session))

    embedder = CountingEmbedder(model_name="another-model")
    await ingest_policies(session, policies, embedder)

    assert len(embedder.embedded) == total


async def test_a_failure_leaves_no_rows(session: AsyncSession, policy_dir: Path) -> None:
    # A savepoint stands in for the script's single transaction.
    with pytest.raises(RuntimeError, match="model crashed"):
        async with session.begin_nested():
            await ingest_policies(session, load_policy_directory(policy_dir), BrokenEmbedder())

    assert (await session.scalars(select(PolicyDocument))).all() == []
    assert (await session.scalars(select(PolicyChunk))).all() == []


async def test_embeddings_have_a_cosine_hnsw_index(session: AsyncSession) -> None:
    definition = await session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_policy_chunks_embedding_hnsw'")
    )
    assert definition is not None
    assert "hnsw" in definition
    assert "vector_cosine_ops" in definition
