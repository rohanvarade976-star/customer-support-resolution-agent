"""Seed the demo scenarios into the database from DATABASE_URL.

Usage (from backend/):
    uv run python scripts/seed_db.py           # add any missing scenario rows
    uv run python scripts/seed_db.py --reset   # delete the scenario rows
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from support_agent.db.seed import ResetRefusedError, load_scenarios, reset, seed
from support_agent.db.session import get_engine, get_sessionmaker


async def run(do_reset: bool) -> dict[str, int]:
    data = load_scenarios()
    try:
        # session.begin() commits on success and rolls back on any error.
        async with get_sessionmaker()() as session, session.begin():
            if do_reset:
                return await reset(session, data)
            return await seed(session, data, now=datetime.now(UTC))
    finally:
        await get_engine().dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="delete the seeded rows instead")
    args = parser.parse_args()

    try:
        counts = asyncio.run(run(args.reset))
    except ResetRefusedError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print("Rows deleted:" if args.reset else "Rows created:")
    for table, count in counts.items():
        print(f"  {table:<20} {count}")
    print(f"  {'total':<20} {sum(counts.values())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
