"""Reset the demo database to its starting state, ready for another demo run.

Deletes what demos and workflow runs created (tickets, runs, proposals, approvals,
refund operations, audit events, and the mock payment and ticketing records), then
seeds the scenarios again from the demo anchor date, so every scenario matches the PRD
(for example Priya's refund is eligible again). Refuses to run when APP_ENV=production.

Usage (from backend/):
    uv run python scripts/reset_demo.py

Needs DATABASE_URL and DEMO_PASSWORD in .env. Policies are not touched.
"""

import asyncio
import sys
from datetime import UTC, datetime

from support_agent.core.config import get_settings
from support_agent.core.demo_anchor import resolve_demo_now
from support_agent.core.security import hash_password
from support_agent.db.demo_reset import DemoResetReport, reset_demo
from support_agent.db.seed import ResetRefusedError, load_scenarios
from support_agent.db.session import get_engine, get_sessionmaker


async def run(password_hash: str) -> DemoResetReport:
    settings = get_settings()
    now = resolve_demo_now(settings.demo_anchor_date, datetime.now(UTC))
    try:
        # session.begin() commits on success and rolls back on any error.
        async with get_sessionmaker()() as session, session.begin():
            return await reset_demo(session, load_scenarios(), now, password_hash)
    finally:
        await get_engine().dispose()


def print_counts(title: str, counts: dict[str, int]) -> None:
    print(title)
    for table, count in counts.items():
        print(f"  {table:<28} {count}")
    print(f"  {'total':<28} {sum(counts.values())}")


def main() -> int:
    demo_password = get_settings().demo_password
    if demo_password is None:
        print("Error: set DEMO_PASSWORD in .env to seed the demo accounts.", file=sys.stderr)
        return 1
    try:
        report = asyncio.run(run(hash_password(demo_password.get_secret_value())))
    except ResetRefusedError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print_counts("Rows deleted:", report.deleted)
    print_counts("Rows created:", report.created)
    print(
        "\nReminder: restart the mock shipping provider now. It fixes its dates when it\n"
        "starts, so a copy started before this reset can disagree with the new data."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
