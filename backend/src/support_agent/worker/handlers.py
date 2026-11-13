"""One handler per job type. The worker (worker/runner.py) claims a job and calls its handler.

Delivery is at-least-once: a worker can die after doing the work but before marking the job
done, and then the job runs again. Every handler is therefore safe to run twice:

- start_run / resume_after_customer_reply / resume_after_decision: the run service skips a
  run that is already paused or finished in our tables, and otherwise continues from the
  last checkpoint (workflow/graph.py `advance_workflow`), never from the beginning.
- The money path keeps its own protections: one open proposal per order (a rerun of the
  proposal step returns this run's existing proposal), one refund operation per proposal
  (a unique constraint), and the compare-and-set claim that moved the run out of its
  waiting state before the job was queued.
- expire_approvals only picks approvals that are still undecided, so a rerun finds nothing
  new to do.
- purge_expired_memory deletes rows whose expiry has passed; a rerun deletes nothing more.

Payloads hold ids only. A handler raises to ask for a retry, or PermanentJobError when
retrying cannot help (the job then goes dead at once and its run is escalated).
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import PermanentJobError
from support_agent.domain.enums import JobType
from support_agent.services import approvals as approvals_service
from support_agent.services import memory as memory_service
from support_agent.services import runs as runs_service
from support_agent.worker.queue import ClaimedJob
from support_agent.workflow.graph import Checkpointer


class WorkflowEnvFactory(Protocol):
    """Opens the clients a segment needs, for the length of one job.

    `llm_enabled=False` gives a disabled LLM client: after a reviewer's decision (or an
    expiry) the run is fixed code only, and no model may be called.
    """

    def __call__(
        self, *, llm_enabled: bool
    ) -> AbstractAsyncContextManager[runs_service.WorkflowEnv]: ...


SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class WorkerDeps:
    settings: Settings
    clock: Callable[[], datetime]
    checkpointer: Checkpointer
    sessions: SessionFactory  # a fresh session per use (the heartbeat needs its own)
    workflow_env: WorkflowEnvFactory

    def enqueue_env(self) -> runs_service.EnqueueEnv:
        return runs_service.EnqueueEnv(self.settings, self.clock, self.checkpointer)


Handler = Callable[[AsyncSession, ClaimedJob, WorkerDeps], Awaitable[None]]


def _run_id(job: ClaimedJob) -> UUID:
    if job.run_id is None:  # the database's CHECK makes this impossible for run jobs
        raise PermanentJobError("JOB_WITHOUT_RUN")
    return job.run_id


def _payload_id(job: ClaimedJob, key: str) -> UUID:
    try:
        return UUID(str(job.payload[key]))
    except (KeyError, ValueError):
        raise PermanentJobError("BAD_PAYLOAD") from None


def _checkpoint_id(job: ClaimedJob) -> str | None:
    value = job.payload.get("checkpoint_id")
    return None if value is None else str(value)


async def start_run(session: AsyncSession, job: ClaimedJob, deps: WorkerDeps) -> None:
    async with deps.workflow_env(llm_enabled=True) as env:
        await runs_service.run_start_job(session, env, _run_id(job))


async def resume_after_customer_reply(
    session: AsyncSession, job: ClaimedJob, deps: WorkerDeps
) -> None:
    message_id = _payload_id(job, "message_id")
    async with deps.workflow_env(llm_enabled=True) as env:
        await runs_service.run_reply_job(
            session, env, _run_id(job), message_id, _checkpoint_id(job)
        )


async def resume_after_decision(session: AsyncSession, job: ClaimedJob, deps: WorkerDeps) -> None:
    async with deps.workflow_env(llm_enabled=False) as env:
        await runs_service.run_decision_job(session, env, _run_id(job), _checkpoint_id(job))


async def expire_approvals(session: AsyncSession, job: ClaimedJob, deps: WorkerDeps) -> None:
    await approvals_service.expire_overdue_approvals(session, deps.enqueue_env())


async def purge_expired_memory(session: AsyncSession, job: ClaimedJob, deps: WorkerDeps) -> None:
    await memory_service.purge_expired(session, now=deps.clock())


HANDLERS: dict[JobType, Handler] = {
    JobType.START_RUN: start_run,
    JobType.RESUME_AFTER_CUSTOMER_REPLY: resume_after_customer_reply,
    JobType.RESUME_AFTER_DECISION: resume_after_decision,
    JobType.EXPIRE_APPROVALS: expire_approvals,
    JobType.PURGE_EXPIRED_MEMORY: purge_expired_memory,
}


async def escalate_dead_job(
    session: AsyncSession, job: ClaimedJob, code: str, deps: WorkerDeps
) -> None:
    """A dead job's run goes to a person, with the reason code. Not committed."""
    if job.run_id is not None:
        await runs_service.escalate_after_dead_job(
            session, deps.settings, job.run_id, job_type=job.job_type, code=code, now=deps.clock()
        )
