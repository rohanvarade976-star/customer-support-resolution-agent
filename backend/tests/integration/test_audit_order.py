"""Audit events keep the order they were written in, even inside one transaction (ADR-019).

audit_events.created_at defaults to clock_timestamp() (migration e2b6c9d4a1f3). With now(), the
transaction's start time, every event of one transaction shared a timestamp and the tie-break
(a random UUID) shuffled them, so a run's timeline could show "escalated" before "classified".
"""

from datetime import UTC, datetime
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import WorkflowRun
from support_agent.db.seed import load_scenarios, seed
from support_agent.repositories import audit

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
# Enough events that random UUIDs landing in this order by chance is practically impossible
# (1 in 30!), so the test fails reliably if the timestamps are equal.
EVENT_COUNT = 30


async def test_events_of_one_transaction_come_back_in_written_order(
    session: AsyncSession, demo_password_hash: str
) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)
    run_id = await session.scalar(select(WorkflowRun.id).limit(1))
    assert run_id is not None
    before = {event.id for event in await audit.list_for_run(session, run_id)}

    # All in the test's single open transaction, like one saved segment of a run.
    written = [f"test.step_{n:02d}" for n in range(EVENT_COUNT)]
    for event_type in written:
        await audit.record(
            session, run_id=run_id, actor="system", event_type=event_type, payload={}
        )

    events = [e for e in await audit.list_for_run(session, run_id) if e.id not in before]
    assert [event.event_type for event in events] == written
    times = [event.created_at for event in events]
    assert all(earlier < later for earlier, later in pairwise(times))
