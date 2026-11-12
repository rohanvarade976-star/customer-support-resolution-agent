"""Embedders turn text into vectors (lists of numbers that represent meaning).

- FastEmbedEmbedder: the real model, BAAI/bge-small-en-v1.5, run locally on the CPU.
- FakeEmbedder: a deterministic stand-in for tests. It needs no download and gives
  the same vector for the same text every time, but the vectors carry no meaning.

Which one is used comes from EMBEDDING_PROVIDER (see core/config.py).

Loading the real model takes seconds, so `get_embedder` creates one embedder per
provider for the whole process and returns that same object every time after.
"""

import asyncio
import hashlib
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol

from support_agent.core.config import Settings
from support_agent.db.models.policies import EMBEDDING_DIMENSIONS

FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
FAKE_MODEL = "fake-sha256"


class Embedder(Protocol):
    model_name: str  # stored with each chunk, so a model change re-embeds everything
    dimensions: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per document text, in the same order."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """One vector for a search query. Some models embed queries differently."""
        ...


class FakeEmbedder:
    model_name = FAKE_MODEL
    dimensions = EMBEDDING_DIMENSIONS

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [fake_vector(text, self.dimensions) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        # Same as a document, so a test can make a query match a chunk exactly.
        return fake_vector(text, self.dimensions)


def fake_vector(text: str, dimensions: int) -> list[float]:
    """A unit-length vector derived from SHA-256 of the text. Same text, same vector."""
    values: list[float] = []
    block = 0
    while len(values) < dimensions:
        digest = hashlib.sha256(f"{block}:{text}".encode()).digest()
        values.extend((byte - 127.5) / 127.5 for byte in digest)  # bytes 0..255 -> -1..1
        block += 1
    values = values[:dimensions]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


class FastEmbedEmbedder:
    def __init__(self) -> None:
        # Imported here, not at the top: fastembed is large, and tests never load it.
        from fastembed import TextEmbedding

        self.model_name = FASTEMBED_MODEL
        self.dimensions = EMBEDDING_DIMENSIONS
        # Downloads the model (~70 MB) on first use, then loads it from fastembed's cache.
        # This is slow, blocking work, so create the embedder before opening a DB transaction.
        self._model = TextEmbedding(model_name=FASTEMBED_MODEL)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        # The model is CPU-bound and synchronous; a worker thread keeps the event loop free.
        return await asyncio.to_thread(self._embed, list(texts))

    def _embed(self, texts: list[str]) -> list[list[float]]:
        # passage_embed (not query_embed): these texts are documents to be searched.
        return [[float(x) for x in vector] for vector in self._model.passage_embed(texts)]

    async def embed_query(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._embed_query, text)

    def _embed_query(self, text: str) -> list[float]:
        # query_embed adds the instruction bge models expect in front of a search query.
        vector = next(iter(self._model.query_embed(text)))
        return [float(x) for x in vector]


def get_embedder(settings: Settings) -> Embedder:
    """The process-wide embedder for the configured provider (created on first use)."""
    return _cached_embedder(settings.embedding_provider)


@lru_cache(maxsize=2)  # at most one per provider: "fake" and "fastembed"
def _cached_embedder(provider: str) -> Embedder:
    if provider == "fake":
        return FakeEmbedder()
    return FastEmbedEmbedder()
