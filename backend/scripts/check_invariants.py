"""Check the money invariants (ADR-015) against the database in DATABASE_URL.

Read-only: the session is rolled back at the end, so nothing is ever written. Prints each
violation as a code and ids (no customer data) and exits with 1 if there is any, 0 if none.
The "stuck" checks skip rows changed within INVARIANT_GRACE_SECONDS (work still in flight);
pass --grace-seconds to override it, e.g. 0 on a system you know is idle.

Usage (from backend/):
    uv run python scripts/check_invariants.py
    uv run python scripts/check_invariants.py --grace-seconds 0
"""

import argparse
import asyncio
import sys

from support_agent.core.config import get_settings
from support_agent.db.session import get_engine, get_sessionmaker
from support_agent.services.invariants import Violation, check_money_invariants


async def run(grace_seconds: int) -> list[Violation]:
    try:
        async with get_sessionmaker()() as session:
            try:
                return await check_money_invariants(session, grace_seconds=grace_seconds)
            finally:
                await session.rollback()  # read-only: never commit anything
    finally:
        await get_engine().dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the money invariants.")
    parser.add_argument("--grace-seconds", type=int, default=None, help="override the grace")
    args = parser.parse_args()
    grace = (
        get_settings().invariant_grace_seconds if args.grace_seconds is None else args.grace_seconds
    )
    if grace < 0:
        parser.error("--grace-seconds must be 0 or more")

    violations = asyncio.run(run(grace))
    if not violations:
        print(f"OK: no money invariant violations (grace {grace} s).")
        return 0
    print(f"{len(violations)} money invariant violation(s) (grace {grace} s):")
    for violation in violations:
        ids = " ".join(f"{name}={value}" for name, value in violation.ids.items())
        print(f"  {violation.code.value:<32} {ids}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
