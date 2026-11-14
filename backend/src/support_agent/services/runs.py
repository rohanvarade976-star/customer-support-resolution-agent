"""Starting, resuming and reading workflow runs.

A run is driven in segments (workflow/graph.py): start -> pause -> resume -> ... -> end.
The checkpointer keeps the run's place between segments, keyed by `workflow_runs.thread_id`.

Since M7 Task 2 the API never runs the graph itself. Each request does its checks, changes
the run, and adds a job in the SAME transaction (worker/queue.py), so the run and its job are
committed together or not at all. It answers 202; the background worker runs the segment.

API side (the request):

- start_run (POST /tickets/{id}/runs): check the caller owns the ticket (anyone else's is a
  404, like a missing one), create the run, record `run.created`, enqueue `start_run`. A
  second unfinished run on the ticket is refused with 409, even when two requests race: the
  database's partial unique index lets only one insert through.
- post_customer_message (POST /runs/{id}/messages) and the approval decision
  (services/approvals.py) claim the paused run: one UPDATE moves it out of its waiting
  status, or matches nothing (409). Only one request can win. `run.resumed` records who
  resumed it. The reply is stored in customer_messages; the job carries only its id, plus
  the id of the checkpoint the run is paused at (see `advance_workflow`).

Worker side (worker/handlers.py calls these):

- run_start_job / run_reply_job / run_decision_job run one segment with `advance_workflow`,
  which continues from the last checkpoint if an earlier attempt died part-way. A run that
  is already paused or finished in our tables is left alone, so a repeated job does nothing.
- After the segment, services/run_recording.py saves what it did and it is committed. If
  anything breaks, the uncommitted work is rolled back and the exception goes to the worker,
  which retries the job; when its attempts run out the run is escalated
  (escalate_after_dead_job).

The customer id given to the workflow is the ticket's owner, which was matched to the JWT.
It never comes from the complaint text, a reply, or the model.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agents.resolve import ESCALATION_MESSAGE
from support_agent.core.config import Settings
from support_agent.core.errors import AppError, PermanentJobError
from support_agent.core.ids import std_uuid
from support_agent.db.models import Job, Ticket, WorkflowRun
from support_agent.domain.enums import (
    ApprovalDecision,
    JobStatus,
    JobType,
    MemoryKind,
    MemoryOutcome,
    Role,
    RunMode,
    RunStatus,
    TicketStatus,
)
from support_agent.integrations.payments import PaymentClient
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import LLMClient
from support_agent.llm.usage import metered
from support_agent.rag.embeddings import Embedder
from support_agent.repositories import audit, customer_messages, proposals, tickets, workflow_runs
from support_agent.repositories.tickets import TicketRecord
from support_agent.schemas.auth import Principal
from support_agent.schemas.runs import RunJob, RunResponse
from support_agent.services import eligibility, memory, order_status, refund_execution
from support_agent.services import orders as orders_service
from support_agent.services import proposals as proposal_service
from support_agent.services.case_updates import set_run_status, set_ticket_status
from support_agent.services.orders import customer_owns_order
from support_agent.services.run_recording import save_segment
from support_agent.services.tickets import ticket_not_found
from support_agent.tools.base import ToolContext
from support_agent.tools.read_tools import build_read_registry
from support_agent.worker import queue
from support_agent.workflow.deps import BranchSessions, CaseFacts, RefundResult, WorkflowDeps
from support_agent.workflow.graph import (
    Checkpointer,
    NotPausedError,
    advance_workflow,
    initial_state,
    latest_checkpoint_id,
)
from support_agent.workflow.state import WorkflowState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnqueueEnv:
    """What a request needs to queue work: settings, the clock, and the checkpointer (read
    only, for the id of the checkpoint a paused run waits at). No model, no providers."""

    settings: Settings
    clock: Callable[[], datetime]
    checkpointer: Checkpointer


@dataclass(frozen=True)
class WorkflowEnv:
    """Everything a segment needs from outside: clients, the clock, sessions, the checkpointer.

    Built per job by the worker (worker/main.py). Decision jobs get a disabled LLM client.
    """

    settings: Settings
    llm: LLMClient
    shipping_client: ShippingClient
    payments: PaymentClient
    embedder: Embedder
    clock: Callable[[], datetime]
    branch_sessions: BranchSessions
    checkpointer: Checkpointer


def run_not_found() -> AppError:
    return AppError(404, "RUN_NOT_FOUND", "Run not found.")


def not_paused() -> AppError:
    return AppError(409, "RUN_NOT_PAUSED", "This run is not waiting for this input.")


def _already_active() -> AppError:
    return AppError(409, "RUN_ALREADY_ACTIVE", "This ticket already has a run in progress.")


def _to_response(run: WorkflowRun, ticket: Ticket, job: Job | None) -> RunResponse:
    return RunResponse(
        run_id=run.id,
        ticket_id=ticket.id,
        status=run.status,
        category=ticket.category,
        started_at=run.started_at,
        completed_at=run.completed_at,
        job=None if job is None else _job_view(job),
    )


def _job_view(job: Job) -> RunJob:
    return RunJob(
        job_type=job.job_type,
        status=job.status,
        attempts=job.attempts,
        next_attempt_at=job.available_at if job.status == JobStatus.QUEUED else None,
    )


# ---------------------------------------------------------------- API side: queue the work


async def start_run(
    session: AsyncSession,
    principal: Principal,
    ticket_id: UUID,
    env: EnqueueEnv,
    mode: RunMode = RunMode.MULTI_AGENT,
) -> RunResponse:
    record = await tickets.get_by_id(session, ticket_id)
    if record is None or record.ticket.customer_id != principal.id:
        raise ticket_not_found()
    ticket = record.ticket
    if await workflow_runs.has_unfinished_run(session, ticket.id):
        raise _already_active()  # the common case, answered without touching the index

    try:
        run = await workflow_runs.create_agent_run(
            session, ticket_id=ticket.id, mode=mode, now=env.clock()
        )
    except workflow_runs.RunAlreadyActiveError:
        await session.rollback()  # another request created one a moment ago
        raise _already_active() from None
    await audit.record(
        session,
        run_id=run.id,
        actor="customer",
        event_type="run.created",
        payload={"run_id": str(run.id), "ticket_id": str(ticket.id)},
    )
    job = await queue.enqueue(
        session,
        JobType.START_RUN,
        run_id=run.id,
        payload={},
        now=env.clock(),
        max_attempts=env.settings.job_max_attempts,
    )
    await session.commit()  # the run and its job, together
    return _to_response(run, ticket, job)


async def post_customer_message(
    session: AsyncSession, env: EnqueueEnv, principal: Principal, run_id: UUID, text: str
) -> RunResponse:
    """The customer's reply to a run's question. Owner only; the text is untrusted data."""
    run = await workflow_runs.get(session, run_id)
    record = None if run is None else await tickets.get_by_id(session, run.ticket_id)
    if run is None or record is None or record.ticket.customer_id != principal.id:
        raise run_not_found()  # someone else's run looks exactly like a missing one
    ticket = record.ticket

    await claim_paused_run(
        session,
        run,
        waiting=RunStatus.AWAITING_CUSTOMER,
        resuming=RunStatus.INVESTIGATING,
        resumed_by="customer",
        actor_id=principal.id,
    )
    message = await customer_messages.create(
        session, run_id=run.id, ticket_id=ticket.id, customer_id=principal.id, body=text
    )
    job = await enqueue_resume(
        session,
        env,
        run,
        JobType.RESUME_AFTER_CUSTOMER_REPLY,
        {"message_id": str(message.id)},
    )
    await session.commit()  # the claim, the message and the job, together
    return _to_response(run, ticket, job)


async def claim_paused_run(
    session: AsyncSession,
    run: WorkflowRun,
    *,
    waiting: RunStatus,
    resuming: RunStatus,
    resumed_by: str,
    actor_id: UUID | None,
) -> None:
    """Take the paused run, or refuse with 409. Not committed: the caller commits.

    `actor_id` is None when the system resumes it (an approval that expired).
    """
    if run.mode == RunMode.MANUAL:  # manual (M3) runs have no graph to resume
        raise not_paused()
    if not await workflow_runs.claim(session, run, expected=waiting, new=resuming):
        raise not_paused()
    await audit.record(
        session,
        run_id=run.id,
        actor=resumed_by,
        event_type="run.resumed",
        payload={
            "run_id": str(run.id),
            "reason": waiting.value,
            "resumed_by": resumed_by,
            "actor_id": None if actor_id is None else str(actor_id),
        },
    )


async def enqueue_resume(
    session: AsyncSession,
    env: EnqueueEnv,
    run: WorkflowRun,
    job_type: JobType,
    payload: dict[str, Any],
) -> Job:
    """Queue the resume of a run the caller has just claimed. Not committed.

    The job also carries the id of the checkpoint the run is paused at, so a retried job
    can tell its own pause from a later one (workflow/graph.py `advance_workflow`).
    """
    checkpoint_id = await latest_checkpoint_id(env.checkpointer, run.thread_id)
    return await queue.enqueue(
        session,
        job_type,
        run_id=run.id,
        payload={**payload, "checkpoint_id": checkpoint_id},
        now=env.clock(),
        max_attempts=env.settings.job_max_attempts,
    )


# ---------------------------------------------------------------- worker side: run a segment


async def _load_run(session: AsyncSession, run_id: UUID) -> tuple[WorkflowRun, TicketRecord]:
    run = await workflow_runs.get(session, run_id)
    record = None if run is None else await tickets.get_by_id(session, run.ticket_id)
    if run is None or record is None:
        raise PermanentJobError("RUN_NOT_FOUND")
    await session.refresh(run)  # never act on a copy from before an earlier rollback
    return run, record


def _nothing_to_do(run: WorkflowRun) -> bool:
    """Paused or finished in our tables: an earlier attempt of this job already saved it."""
    done = run.status in workflow_runs.FINISHED_STATUSES | workflow_runs.WAITING_STATUSES
    if done:
        log.info("job_already_done", extra={"run_id": str(run.id), "status": run.status.value})
    return done


async def run_start_job(session: AsyncSession, env: WorkflowEnv, run_id: UUID) -> None:
    run, record = await _load_run(session, run_id)
    if _nothing_to_do(run):
        return
    ticket = record.ticket
    state = initial_state(
        run_id=run.id,
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        complaint_text=ticket.summary,
        ticket_order_number=record.order_number,
    )
    await _advance(session, env, run, ticket, initial=state, resume=None)


async def run_reply_job(
    session: AsyncSession,
    env: WorkflowEnv,
    run_id: UUID,
    message_id: UUID,
    checkpoint_id: str | None,
) -> None:
    """Resume a run with the customer's stored reply, after checking it again.

    The request already checked ownership. The job row is not trusted alone, so the message
    must belong to this run, this ticket, and the ticket's owner; anything else can never
    succeed and the job goes dead (the run is escalated).
    """
    run, record = await _load_run(session, run_id)
    if _nothing_to_do(run):
        return
    ticket = record.ticket
    message = await customer_messages.get(session, message_id)
    owned = (
        message is not None
        and message.run_id == run.id
        and message.ticket_id == ticket.id
        and message.customer_id == ticket.customer_id
    )
    if message is None or not owned:
        raise PermanentJobError("MESSAGE_NOT_OWNED")
    await _advance(
        session, env, run, ticket, initial=None, resume=(checkpoint_id, {"text": message.body})
    )


async def run_decision_job(
    session: AsyncSession, env: WorkflowEnv, run_id: UUID, checkpoint_id: str | None
) -> None:
    """Resume a run after a reviewer's decision or an expiry.

    The resume value carries nothing that decides: the graph reads the stored decision.
    """
    run, record = await _load_run(session, run_id)
    if _nothing_to_do(run):
        return
    resume = (checkpoint_id, {"event": "decided"})
    await _advance(session, env, run, record.ticket, initial=None, resume=resume)


async def _advance(
    session: AsyncSession,
    env: WorkflowEnv,
    run: WorkflowRun,
    ticket: Ticket,
    *,
    initial: WorkflowState | None,
    resume: tuple[str | None, dict[str, str]] | None,
) -> None:
    """Run one segment from wherever the checkpoints say, save what it did, and commit.

    On failure, roll back what the segment had not committed and re-raise: the worker
    decides between retrying the job and giving up.
    """
    deps = _build_deps(session, env, run, ticket)
    try:
        with metered() as meter:
            segment = await advance_workflow(
                deps, env.checkpointer, run.thread_id, initial=initial, resume=resume
            )
        await save_segment(session, env.settings, run, ticket, segment, meter, env.clock())
        await session.commit()
    except NotPausedError:
        await session.rollback()
        raise PermanentJobError("RUN_NOT_RESUMABLE") from None  # no checkpoint to resume
    except BaseException:
        await session.rollback()
        raise
    log.info("run_segment_finished", extra={"run_id": str(run.id), "status": run.status.value})


async def escalate_after_dead_job(
    session: AsyncSession,
    settings: Settings,
    run_id: UUID,
    *,
    job_type: JobType,
    code: str,
    now: datetime,
) -> None:
    """A job for this run will never succeed: hand the case to a person. Not committed.

    A run that has already finished is left as it is. A refund operation the run started
    keeps its own status; reconciling unknown refund outcomes is M8's job.
    """
    run = await workflow_runs.get(session, run_id)
    record = None if run is None else await tickets.get_by_id(session, run.ticket_id)
    if run is None or record is None:
        return
    await session.refresh(run)
    if run.status in workflow_runs.FINISHED_STATUSES:
        return
    ticket = record.ticket
    await set_run_status(
        session,
        run,
        RunStatus.ESCALATED,
        now=now,
        actor="system",
        details={"reason": "JOB_DEAD", "job_type": job_type.value, "error_code": code},
    )
    await set_ticket_status(session, ticket, TicketStatus.ESCALATED, run_id=run.id, actor="system")
    await tickets.set_reply(session, ticket, ESCALATION_MESSAGE)
    await memory.record_run_outcome(
        session,
        settings,
        run=run,
        ticket=ticket,
        kind=MemoryKind.CASE_ESCALATED,
        outcome=MemoryOutcome.ESCALATED,
        order_number=None,  # no state to read a verified hint from: the ticket's order only
        now=now,
    )


def _build_deps(
    session: AsyncSession, env: WorkflowEnv, run: WorkflowRun, ticket: Ticket
) -> WorkflowDeps:
    settings, clock = env.settings, env.clock

    async def owns_order(customer_id: UUID, order_number: str) -> bool:
        return await customer_owns_order(session, customer_id, order_number)

    async def load_case_facts(customer_id: UUID, order_number: str, now: datetime) -> CaseFacts:
        decision = await eligibility.check_refund_eligibility(
            session, customer_id, order_number, now
        )
        threshold = await order_status.get_investigation_threshold(
            session, customer_id, order_number
        )
        return CaseFacts(decision, threshold)

    async def load_memory(customer_id: UUID) -> str:
        return await memory.load_for_run(session, settings, customer_id, now=clock())

    async def load_owned_order_numbers(customer_id: UUID) -> list[str]:
        return await orders_service.list_order_numbers(session, customer_id)

    async def propose_refund(order_number: str, amount_minor: int) -> UUID:
        proposal_id = await proposal_service.create_proposal_for_run(
            session,
            ticket,
            run.id,
            order_number=order_number,
            expected_amount_minor=amount_minor,
            now=clock(),
            settings=settings,
        )
        # Committed now, before the checkpointer saves this step (ADR-012). Otherwise a
        # worker dying in between would leave a checkpoint pointing at a proposal that was
        # rolled back. A rerun of this step gets the same proposal back.
        await session.commit()
        return std_uuid(proposal_id)  # it goes into the workflow state

    async def load_approval_decision(proposal_id: UUID) -> ApprovalDecision | None:
        approval = await proposals.get_approval_for_proposal(session, proposal_id)
        return None if approval is None else approval.decision

    async def execute_refund(proposal_id: UUID) -> RefundResult:
        operation = await refund_execution.execute_approved_refund(
            session, env.payments, proposal_id, now=clock(), settings=settings
        )
        return RefundResult(operation.status, refund_execution.describe(operation.status))

    # The tools' customer is the ticket's owner, already matched to the JWT.
    tool_context = ToolContext(
        session, ticket.customer_id, settings, env.shipping_client, env.embedder, clock=clock
    )
    return WorkflowDeps(
        llm=env.llm,
        owns_order=owns_order,
        confidence_threshold=settings.classifier_confidence_threshold,
        registry=build_read_registry(),
        tool_context=tool_context,
        max_tool_calls=settings.investigator_max_tool_calls,
        branch_sessions=env.branch_sessions,
        load_case_facts=load_case_facts,
        load_memory=load_memory,
        load_owned_order_numbers=load_owned_order_numbers,
        propose_refund=propose_refund,
        load_approval_decision=load_approval_decision,
        execute_refund=execute_refund,
        max_clarification_rounds=settings.max_clarification_rounds,
        mode=run.mode,
    )


# ---------------------------------------------------------------- reading


async def get_run(session: AsyncSession, principal: Principal, run_id: UUID) -> RunResponse:
    run = await workflow_runs.get(session, run_id)
    record = None if run is None else await tickets.get_by_id(session, run.ticket_id)
    if run is None or record is None:
        raise run_not_found()
    owns_it = principal.role == Role.CUSTOMER and record.ticket.customer_id == principal.id
    is_staff = principal.role in (Role.REVIEWER, Role.ADMIN)
    if not (owns_it or is_staff):
        raise run_not_found()  # same answer as "no such run"
    return _to_response(run, record.ticket, await queue.latest_for_run(session, run.id))
