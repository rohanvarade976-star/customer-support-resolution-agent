"""Measure policy retrieval quality with the real embedder against DATABASE_URL.

Runs every question in evals/datasets/retrieval_questions.json through the same service
the search_policy tool uses, and prints:
- hit@1: the expected section is the first passage
- hit@3: the expected section is in the first three passages
- every miss, with what came back instead.

Needs a seeded database with ingested policies:
    uv run python scripts/seed_db.py
    uv run python scripts/ingest_policies.py
    uv run python scripts/eval_retrieval.py

Not run in CI: it uses the real embedding model. It only reads; nothing is written.
"""

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from support_agent.core.config import get_settings
from support_agent.core.errors import AppError
from support_agent.db.session import get_engine, get_sessionmaker
from support_agent.rag.embeddings import FAKE_MODEL, Embedder, get_embedder
from support_agent.repositories import users
from support_agent.schemas.policies import PolicySearchResult
from support_agent.services.policy_search import search_policy

DEFAULT_DATASET = (
    Path(__file__).resolve().parents[1] / "evals" / "datasets" / "retrieval_questions.json"
)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    customer_email: str
    order_number: str | None = None
    as_of: date | None = None  # only for questions without an order
    query: str
    expected_chunk_key: str


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    questions: list[Question]


class Outcome(BaseModel):
    question: Question
    result: PolicySearchResult | None
    error: str | None = None

    @property
    def citations(self) -> list[str]:
        return [] if self.result is None else [p.citation for p in self.result.passages]


def _search_time(question: Question) -> datetime:
    if question.as_of is None:
        return datetime.now(UTC)
    return datetime.combine(question.as_of, time(12, 0), tzinfo=UTC)


async def run(questions: list[Question], embedder: Embedder) -> list[Outcome]:
    settings = get_settings()
    outcomes: list[Outcome] = []
    try:
        async with get_sessionmaker()() as session:
            for question in questions:
                customer = await users.get_customer_by_email(session, question.customer_email)
                if customer is None:
                    outcomes.append(Outcome(question=question, result=None, error="no customer"))
                    continue
                try:
                    result = await search_policy(
                        session,
                        embedder,
                        settings,
                        customer.id,
                        question.query,
                        question.order_number,
                        _search_time(question),
                    )
                except AppError as error:
                    outcomes.append(Outcome(question=question, result=None, error=error.error_code))
                    continue
                outcomes.append(Outcome(question=question, result=result))
            await session.rollback()  # read-only: make sure nothing is ever written
    finally:
        await get_engine().dispose()
    return outcomes


def report(outcomes: list[Outcome]) -> None:
    total = len(outcomes)
    hit1 = sum(o.citations[:1] == [o.question.expected_chunk_key] for o in outcomes)
    hit3 = sum(o.question.expected_chunk_key in o.citations[:3] for o in outcomes)
    empty = sum(o.result is not None and o.result.no_relevant_evidence for o in outcomes)
    print(f"Questions: {total}")
    print(f"  hit@1: {hit1}/{total} ({hit1 / total:.0%})")
    print(f"  hit@3: {hit3}/{total} ({hit3 / total:.0%})")
    print(f"  no_relevant_evidence: {empty}")

    misses = [o for o in outcomes if o.citations[:1] != [o.question.expected_chunk_key]]
    if not misses:
        return
    print("\nMisses (not first):")
    for outcome in misses:
        q = outcome.question
        print(f"- {q.id}: expected {q.expected_chunk_key}")
        result = outcome.result
        if result is None:
            print(f"    error: {outcome.error}")
            continue
        print(f"    as_of {result.as_of_date}, query: {q.query!r}")
        for rank, passage in enumerate(result.passages[:3], start=1):
            print(
                f"    {rank}. {passage.citation:<36} similarity {passage.vector_similarity:.3f}"
                f"  rrf {passage.rrf_score:.4f}"
            )
        if result.no_relevant_evidence:
            print("    (nothing passed the similarity threshold)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()

    dataset = Dataset.model_validate_json(args.dataset.read_text(encoding="utf-8"))
    embedder = get_embedder(get_settings())  # loads the model once, before any DB work
    if embedder.model_name == FAKE_MODEL:
        print("Error: EMBEDDING_PROVIDER is 'fake'; this eval needs the real model.")
        return 1
    print(f"Embedding model: {embedder.model_name}")
    print(f"Min similarity:  {get_settings().policy_min_similarity}\n")

    report(asyncio.run(run(dataset.questions, embedder)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
