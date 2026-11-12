"""Load the policy documents in data/policies/ into the database from DATABASE_URL.

Usage (from backend/):
    uv run python scripts/ingest_policies.py
    uv run python scripts/ingest_policies.py --dir path/to/policies

Safe to run any number of times: unchanged sections are skipped.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from support_agent.core.config import get_settings
from support_agent.db.session import get_engine, get_sessionmaker
from support_agent.rag.documents import PolicyFile, PolicyFormatError, load_policy_directory
from support_agent.rag.embeddings import Embedder, get_embedder
from support_agent.rag.ingest import IngestSummary, ingest_policies

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "data" / "policies"


async def run(policies: list[PolicyFile], embedder: Embedder) -> IngestSummary:
    try:
        # One transaction: commits on success, rolls back everything on any error.
        async with get_sessionmaker()() as session, session.begin():
            return await ingest_policies(session, policies, embedder)
    finally:
        await get_engine().dispose()


def print_summary(summary: IngestSummary, embedder: Embedder) -> None:
    print(f"Embedding model: {embedder.model_name}")
    print(f"  {'':<11} {'created':>8} {'updated':>8} {'unchanged':>10} {'deleted':>8}")
    print(
        f"  {'documents':<11} {summary.documents_created:>8} {summary.documents_updated:>8} "
        f"{summary.documents_unchanged:>10} {'-':>8}"
    )
    print(
        f"  {'chunks':<11} {summary.chunks_created:>8} {summary.chunks_updated:>8} "
        f"{summary.chunks_unchanged:>10} {summary.chunks_deleted:>8}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="folder of policy files")
    args = parser.parse_args()

    # Check every file before touching the database.
    try:
        policies = load_policy_directory(args.dir)
    except PolicyFormatError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Loaded {len(policies)} policy files from {args.dir}")

    # Load the model first (may download it), so no transaction is open during a download.
    embedder = get_embedder(get_settings())
    try:
        summary = asyncio.run(run(policies, embedder))
    except PolicyFormatError as error:  # e.g. a badly numbered section, found while chunking
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print_summary(summary, embedder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
