"""The background worker (worker/runner.py, worker/handlers.py) with the real workflow.

The LLM is the scripted `AgentFake`; the checkpointer, tools, eligibility, proposals, the
approval service and the mock payment provider are real. A worker "crash" is a handler that
raises and is then simply abandoned: nothing marks the job done, exactly as if the process
had died. Money and escalations are asserted on database rows.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mock_providers.payments.testing import InMemoryPayments
from support_agent.agents.fake import (
    AgentFake,
    critique_reply,
    draft_reply,
    policy_run,
    simple_investigation,
    supervisor_reply,
)
from support_agent.api.deps import get_clock, get_payment_client
from support_agent.core.config import Settings, get_settings
from support_agent.db.models import (
    ActionProposal,
    Approval,
    AuditEvent,
    Customer,
    Job,
    RefundOperation,
    Ticket,
    WorkflowRun,
)
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import (
    ApprovalDecision,
    JobStatus,
    JobType,
    ProposalStatus,
    RunStatus,
    TicketStatus,
)
from support_agent.integrations.payments import PaymentClient
from support_agent.llm.fake import ScriptStep, tool_reply
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.services import eligibility, policy_search
from support_agent.services import runs as runs_service
from support_agent.worker import handlers, queue, runner
from support_agent.worker.handlers import WorkerDeps

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
HeadersFor = Callable[[str], Awaitable[dict[str, str]]]
UseAgents = Callable[[dict[str, list[ScriptStep]]], AgentFake]
Drain = Callable[[], Awaitable[int]]
BuildDeps = Callable[[], WorkerDeps]


class MovableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture(autouse=True)
def wire_app(app: FastAPI, payments: InMemoryPayments, clock: MovableClock) -> Settings:
    """The app's clock and payment provider; no backoff; three attempts per job."""
    app.dependency_overrides[get_clock] = lambda: clock

    async def payment_client() -> AsyncIterator[PaymentClient]:
        async with payments.client() as client:
            yield client

    app.dependency_overrides[get_payment_client] = payment_client
    settings = get_settings().model_copy(
        update={
            "policy_min_similarity": -1.0,  # the fake embedder has no meaning
            "job_max_attempts": 3,
            "job_backoff_base_seconds": 0.0,  # retries are due at once, so drain runs them
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str, ingested_policies: None) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


# ---------------------------------------------------------------- helpers


async def priya_scripts(session: AsyncSession, settings: Settings) -> dict[str, list[ScriptStep]]:
    priya_id = await session.scalar(
        select(Customer.id).where(Customer.email == "priya@example.com")
    )
    assert priya_id is not None
    found = await policy_search.search_policy(
        session, FakeEmbedder(), settings, priya_id, "return window", "ORD-1001", NOW
    )
    citation = found.passages[0].citation
    return {
        "classifier": [
            tool_reply("classification", {"category": "refund", "confidence": 0.95, "reason": "t"})
        ],
        "supervisor": [supervisor_reply()],
        "investigator": simple_investigation("ORD-1001"),
        "policy": policy_run(citation),
        "resolution": [draft_reply("refund", citations=[citation])],
        "critic": [critique_reply("approve")],
    }


async def queue_priya_run(client: httpx.AsyncClient, auth_headers: HeadersFor) -> tuple[UUID, UUID]:
    """Priya's refund complaint, started through the API: queued, not run. (ticket, run)."""
    priya = await auth_headers("priya@example.com")
    created = await client.post(
        "/tickets",
        json={"message": "Where is my refund?", "order_number": "ORD-1001"},
        headers=priya,
    )
    ticket_id = UUID(created.json()["id"])
    started = await client.post(f"/tickets/{ticket_id}/runs", headers=priya)
    assert started.status_code == 202
    return ticket_id, UUID(started.json()["run_id"])


async def reload[M](session: AsyncSession, model: type[M], row_id: UUID) -> M:
    row = await session.get(model, row_id, populate_existing=True)
    assert row is not None
    return row


async def proposals_for(session: AsyncSession, run_id: UUID) -> list[ActionProposal]:
    stmt = select(ActionProposal).where(ActionProposal.run_id == run_id)
    return list((await session.scalars(stmt)).all())


async def jobs_for(session: AsyncSession, run_id: UUID) -> list[Job]:
    stmt = (
        select(Job)
        .where(Job.run_id == run_id)
        .order_by(Job.created_at)
        .execution_options(populate_existing=True)
    )
    return list((await session.scalars(stmt)).all())


async def events(session: AsyncSession, run_id: UUID, event_type: str) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(AuditEvent).where(AuditEvent.run_id == run_id, AuditEvent.event_type == event_type)
    )
    return [row.redacted_payload for row in rows]


def calls_per_agent(fake: AgentFake) -> dict[str, int]:
    return {agent: len(calls) for agent, calls in fake.requests_by_agent.items()}


# Every scripted step used exactly once. A second pass would run out of script (the fake then
# fails the call and the run escalates), so equal counts mean no step ran twice.
ONE_CALL_EACH = {
    "classifier": 1,
    "supervisor": 1,
    "investigator": 4,  # simple_investigation: plan, a read, "done", summary
    "policy": 3,  # policy_run: a search, "done", findings
    "resolution": 1,
    "critic": 1,
}


# ---------------------------------------------------------------- crash and reclaim


async def test_a_crashed_job_is_reclaimed_and_continues_with_exactly_one_proposal(
    client: httpx.AsyncClient,
    session: AsyncSession,
    clock: MovableClock,
    worker_deps: BuildDeps,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = use_agents(await priya_scripts(session, wire_app))
    _, run_id = await queue_priya_run(client, auth_headers)

    # Worker A claims the job and runs the whole segment up to the approval pause, then dies
    # before saving it: the proposal was committed, the run's own row was not.
    def worker_dies(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("worker process killed")

    monkeypatch.setattr(runs_service, "save_segment", worker_dies)
    deps = worker_deps()
    crashed = await runner.claim_next(deps)
    assert crashed is not None and crashed.attempt == 1
    with pytest.raises(RuntimeError):
        await handlers.start_run(session, crashed, deps)
    monkeypatch.undo()
    assert len(await proposals_for(session, run_id)) == 1  # committed before the checkpoint
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.CREATED

    # Nobody can take the job while A's lease could still be alive.
    assert await runner.claim_next(worker_deps()) is None

    # A never comes back. Once the lease has run out, worker B takes the job over.
    clock.now = NOW + timedelta(seconds=wire_app.job_lease_seconds + 1)
    reclaimed = await runner.claim_next(worker_deps())
    assert reclaimed is not None and (reclaimed.id, reclaimed.attempt) == (crashed.id, 2)
    await runner.execute(reclaimed, worker_deps(), heartbeat=False)

    run = await reload(session, WorkflowRun, run_id)
    assert run.status == RunStatus.AWAITING_APPROVAL
    (proposal,) = await proposals_for(session, run_id)  # still exactly one
    approvals = (
        await session.scalars(select(Approval).where(Approval.proposal_id == proposal.id))
    ).all()
    assert len(approvals) == 1
    assert calls_per_agent(fake) == ONE_CALL_EACH  # nothing was asked of the model twice
    (job,) = await jobs_for(session, run_id)
    assert (job.status, job.attempts) == (JobStatus.SUCCEEDED, 2)

    # A wakes up after all and tries to finish its attempt: the fencing token refuses it.
    assert not await queue.complete(session, crashed)
    assert (await jobs_for(session, run_id))[0].attempts == 2


async def test_a_retried_job_continues_from_the_checkpoint_not_the_start(
    client: httpx.AsyncClient,
    session: AsyncSession,
    drain_jobs: Drain,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = use_agents(await priya_scripts(session, wire_app))
    _, run_id = await queue_priya_run(client, auth_headers)

    # The eligibility lookup (after the agents have gathered evidence) fails once.
    real_check = eligibility.check_refund_eligibility
    failures = iter([RuntimeError("database hiccup")])

    async def flaky_check(*args: Any, **kwargs: Any) -> Any:
        for error in failures:
            raise error
        return await real_check(*args, **kwargs)

    monkeypatch.setattr(eligibility, "check_refund_eligibility", flaky_check)

    assert await drain_jobs() == 2  # the failed attempt, then the retry

    run = await reload(session, WorkflowRun, run_id)
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert len(await proposals_for(session, run_id)) == 1
    # The retry resumed after the last saved step: no agent ran twice.
    assert calls_per_agent(fake) == ONE_CALL_EACH
    (job,) = await jobs_for(session, run_id)
    assert (job.status, job.attempts, job.last_error_code) == (JobStatus.SUCCEEDED, 2, None)


async def test_a_job_that_keeps_failing_goes_dead_and_escalates_its_run(
    client: httpx.AsyncClient,
    session: AsyncSession,
    drain_jobs: Drain,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_agents(await priya_scripts(session, wire_app))
    ticket_id, run_id = await queue_priya_run(client, auth_headers)

    async def always_down(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(eligibility, "check_refund_eligibility", always_down)

    assert await drain_jobs() == wire_app.job_max_attempts

    (job,) = await jobs_for(session, run_id)
    assert (job.status, job.attempts, job.last_error_code) == (
        JobStatus.DEAD,
        wire_app.job_max_attempts,
        "ConnectionError",
    )
    run = await reload(session, WorkflowRun, run_id)
    ticket = await reload(session, Ticket, ticket_id)
    assert run.status == RunStatus.ESCALATED
    assert ticket.status == TicketStatus.ESCALATED
    (escalated,) = await events(session, run_id, "run.escalated")
    assert escalated == {
        "run_id": str(run_id),
        "status": "escalated",
        "reason": "JOB_DEAD",
        "job_type": "start_run",
        "error_code": "ConnectionError",
    }
    assert await proposals_for(session, run_id) == []


async def test_a_reply_job_whose_message_is_not_the_runs_is_never_retried(
    client: httpx.AsyncClient,
    session: AsyncSession,
    drain_jobs: Drain,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
) -> None:
    use_agents(
        {
            "classifier": [
                tool_reply(
                    "classification", {"category": "unclear", "confidence": 0.3, "reason": "t"}
                )
            ]
        }
    )
    arjun = await auth_headers("arjun@example.com")
    created = await client.post("/tickets", json={"message": "Hmm"}, headers=arjun)
    started = await client.post(f"/tickets/{created.json()['id']}/runs", headers=arjun)
    run_id = UUID(started.json()["run_id"])
    await drain_jobs()
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.AWAITING_CUSTOMER

    replied = await client.post(f"/runs/{run_id}/messages", json={"text": "hi"}, headers=arjun)
    assert replied.status_code == 202
    # Someone tampers with the queued job: it now names a message that is not this run's.
    (_, reply_job) = await jobs_for(session, run_id)
    reply_job.payload = {**reply_job.payload, "message_id": str(uuid4())}
    await session.flush()

    assert await drain_jobs() == 1  # no retries: retrying cannot make it the run's message

    (_, reply_job) = await jobs_for(session, run_id)
    assert (reply_job.status, reply_job.attempts, reply_job.last_error_code) == (
        JobStatus.DEAD,
        1,
        "MESSAGE_NOT_OWNED",
    )
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.ESCALATED


# ---------------------------------------------------------------- approval expiry


async def test_an_approval_nobody_decides_expires_and_escalates_the_run(
    client: httpx.AsyncClient,
    session: AsyncSession,
    payments: InMemoryPayments,
    clock: MovableClock,
    drain_jobs: Drain,
    worker_deps: BuildDeps,
    auth_headers: HeadersFor,
    use_agents: UseAgents,
    wire_app: Settings,
) -> None:
    use_agents(await priya_scripts(session, wire_app))
    ticket_id, run_id = await queue_priya_run(client, auth_headers)
    await drain_jobs()
    (proposal,) = await proposals_for(session, run_id)
    approval = await session.scalar(select(Approval).where(Approval.proposal_id == proposal.id))
    assert approval is not None

    # Before its expiry, the sweep leaves it alone.
    await queue.schedule_sweep(session, JobType.EXPIRE_APPROVALS, now=clock(), max_attempts=3)
    assert await drain_jobs() == 1
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.AWAITING_APPROVAL

    # After it, the worker's scheduled sweep expires it and the run resumes into escalation.
    clock.now = NOW + timedelta(minutes=wire_app.approval_ttl_minutes, seconds=1)
    deps = worker_deps()
    await queue.schedule_sweep(session, JobType.EXPIRE_APPROVALS, now=deps.clock(), max_attempts=3)
    assert await drain_jobs() == 2  # the sweep, then the run's resume

    assert (await reload(session, Approval, approval.id)).decision == ApprovalDecision.EXPIRED
    assert (await reload(session, ActionProposal, proposal.id)).status == ProposalStatus.EXPIRED
    assert (await reload(session, WorkflowRun, run_id)).status == RunStatus.ESCALATED
    assert (await reload(session, Ticket, ticket_id)).status == TicketStatus.ESCALATED
    refunds = await session.scalar(
        select(func.count())
        .select_from(RefundOperation)
        .where(RefundOperation.proposal_id == proposal.id)
    )
    assert refunds == 0
    assert await payments.refund_count() == 0
    (resumed,) = await events(session, run_id, "run.resumed")
    assert (resumed["resumed_by"], resumed["actor_id"]) == ("system", None)

    # A reviewer who arrives late finds it already decided.
    late = await client.post(
        f"/approvals/{approval.id}/decision",
        json={"decision": "approve"},
        headers=await auth_headers("anita@example.com"),
    )
    assert late.status_code == 409
    assert await payments.refund_count() == 0
