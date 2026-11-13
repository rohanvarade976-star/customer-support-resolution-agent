"""Seed the demo scenarios into the database from DATABASE_URL.

Usage (from backend/):
    uv run python scripts/seed_db.py           # add any missing scenario rows
    uv run python scripts/seed_db.py --reset   # delete the scenario rows
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from support_agent.core.config import get_settings
from support_agent.core.demo_anchor import resolve_demo_now
from support_agent.core.security import hash_password
from support_agent.db.seed import ResetRefusedError, load_scenarios, reset, seed
from support_agent.db.session import get_engine, get_sessionmaker


async def run(do_reset: bool, password_hash: str) -> dict[str, int]:
    data = load_scenarios()
    try:
        # session.begin() commits on success and rolls back on any error.
        async with get_sessionmaker()() as session, session.begin():
            if do_reset:
                return await reset(session, data)
            now = resolve_demo_now(get_settings().demo_anchor_date, datetime.now(UTC))
            return await seed(session, data, now=now, password_hash=password_hash)
    finally:
        await get_engine().dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="delete the seeded rows instead")
    args = parser.parse_args()

    demo_password = get_settings().demo_password
    if demo_password is None and not args.reset:
        print("Error: set DEMO_PASSWORD in .env to seed the demo accounts.", file=sys.stderr)
        return 1
    password_hash = hash_password(demo_password.get_secret_value()) if demo_password else ""

    try:
        counts = asyncio.run(run(args.reset, password_hash))
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
