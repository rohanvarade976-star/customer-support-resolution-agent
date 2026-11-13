"""Starting, resuming and reading workflow runs.

A run is driven in segments (workflow/graph.py): start -> pause -> resume -> ... -> end.
The checkpointer keeps the run's place between segments, keyed by `workflow_runs.thread_id`.

start_run:
1. Check the caller owns the ticket (anyone else's ticket is a 404, like a missing one).
2. Refuse if the ticket already has an unfinished run (409). A paused run is unfinished.
3. Create the run, record `run.created`, commit.
4. Run the first segment (a background worker takes this over in M7 Task 2).

post_customer_message (POST /runs/{id}/messages) and resume_after_decision (called by the
approval decision) both:
1. Claim the run: one UPDATE moves it out of its waiting status, or matches nothing. Only
   one request can win; every other (a double click, a second reviewer, a resume of a
   finished run) gets 409 and changes nothing. `run.resumed` records who resumed it.
2. Commit, then resume the graph. No database lock is held while the graph runs.

After every segment, services/run_recording.py saves what it did and the result is
committed. If anything unexpected breaks, the run is marked `failed` so it never stays open,
and anything the segment wrote but did not commit is rolled back.

The customer id given to the workflow is the ticket's owner, which was matched to the JWT.
It never comes from the complaint text, a reply, or the model.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.db.models import Ticket, WorkflowRun
from support_agent.domain.enums import ApprovalDecision, Role, RunMode, RunStatus
from support_agent.integrations.payments import PaymentClient
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import LLMClient
from support_agent.llm.usage import metered
from support_agent.rag.embeddings import Embedder
from support_agent.repositories import audit, proposals, tickets, workflow_runs
from support_agent.schemas.auth import Principal
from support_agent.schemas.runs import RunResponse
from support_agent.services import eligibility, order_status, refund_execution
from support_agent.services import proposals as proposal_service
from support_agent.services.case_updates import set_run_status
from support_agent.services.orders import customer_owns_order
from support_agent.services.run_recording import save_segment
from support_agent.services.tickets import ticket_not_found
from support_agent.tools.base import ToolContext
from support_agent.tools.read_tools import build_read_registry
from support_agent.workflow.deps import BranchSessions, CaseFacts, RefundResult, WorkflowDeps
from support_agent.workflow.graph import (
    Checkpointer,
    NotPausedError,
    Segment,
    initial_state,
    resume_workflow,
    start_workflow,
)
from support_agent.workflow.state import WorkflowState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowEnv:
    """Everything a run needs from outside: clients, the clock, sessions, the checkpointer.

    Built per request by api/deps.py. The approval decision gets a disabled LLM client.
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


def _to_response(run: WorkflowRun, ticket: Ticket) -> RunResponse:
    return RunResponse(
        run_id=run.id,
        ticket_id=ticket.id,
        status=run.status,
        category=ticket.category,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


async def start_run(
    session: AsyncSession,
    principal: Principal,
    ticket_id: UUID,
    env: WorkflowEnv,
    mode: RunMode = RunMode.MULTI_AGENT,
) -> RunResponse:
    record = await tickets.get_by_id(session, ticket_id)
    if record is None or record.ticket.customer_id != principal.id:
        raise ticket_not_found()
    ticket = record.ticket
    if await workflow_runs.has_unfinished_run(session, ticket.id):
        raise AppError(409, "RUN_ALREADY_ACTIVE", "This ticket already has a run in progress.")

    run = await workflow_runs.create_agent_run(
        session, ticket_id=ticket.id, mode=mode, now=env.clock()
    )
    await audit.record(
        session,
        run_id=run.id,
        actor="customer",
        event_type="run.created",
        payload={"run_id": str(run.id), "ticket_id": str(ticket.id)},
    )
    await session.commit()

    state = initial_state(
        run_id=run.id,
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        complaint_text=ticket.summary,
        ticket_order_number=record.order_number,
    )
    deps = _build_deps(session, env, run, ticket)
    await _run_segment(
        session,
        env,
        run,
        ticket,
        lambda: start_workflow(deps, env.checkpointer, run.thread_id, state),
    )
    return _to_response(run, ticket)


async def post_customer_message(
    session: AsyncSession, env: WorkflowEnv, principal: Principal, run_id: UUID, text: str
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
    await session.commit()

    deps = _build_deps(session, env, run, ticket)
    await _run_segment(
        session,
        env,
        run,
        ticket,
        lambda: resume_workflow(deps, env.checkpointer, run.thread_id, {"text": text}),
    )
    return _to_response(run, ticket)


async def claim_paused_run(
    session: AsyncSession,
    run: WorkflowRun,
    *,
    waiting: RunStatus,
    resuming: RunStatus,
    resumed_by: str,
    actor_id: UUID,
) -> None:
    """Take the paused run, or refuse with 409. Not committed: the caller commits."""
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
            "actor_id": str(actor_id),
        },
    )


async def resume_after_decision(
    session: AsyncSession, env: WorkflowEnv, run: WorkflowRun, ticket: Ticket
) -> WorkflowState:
    """Continue a run the decision endpoint has already claimed and committed.

    The resume value carries nothing that decides: the graph reads the stored decision.
    """
    deps = _build_deps(session, env, run, ticket)
    return await _run_segment(
        session,
        env,
        run,
        ticket,
        lambda: resume_workflow(deps, env.checkpointer, run.thread_id, {"event": "decided"}),
    )


async def _run_segment(
    session: AsyncSession,
    env: WorkflowEnv,
    run: WorkflowRun,
    ticket: Ticket,
    segment_call: Callable[[], Awaitable[Segment]],
) -> WorkflowState:
    """Run one segment, save what it did and commit; on any failure mark the run failed."""
    try:
        with metered() as meter:
            segment = await segment_call()
        await save_segment(session, env.settings, run, ticket, segment, meter, env.clock())
        await session.commit()
    except NotPausedError:
        # Nothing ran. A finished run stays as it was; a claimed run with no checkpoint to
        # resume would otherwise stay open forever, so it is marked failed.
        if run.status not in workflow_runs.FINISHED_STATUSES:
            await _mark_failed(session, run, env.clock())
        raise not_paused() from None
    except Exception:
        await _mark_failed(session, run, env.clock())
        raise
    log.info("run_segment_finished", extra={"run_id": str(run.id), "status": run.status.value})
    return segment.state


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

    async def propose_refund(order_number: str, amount_minor: int) -> UUID:
        return await proposal_service.create_proposal_for_run(
            session,
            ticket,
            run.id,
            order_number=order_number,
            expected_amount_minor=amount_minor,
            now=clock(),
            settings=settings,
        )

    async def load_approval_decision(proposal_id: UUID) -> ApprovalDecision | None:
        approval = await proposals.get_approval_for_proposal(session, proposal_id)
        return None if approval is None else approval.decision

    async def execute_refund(proposal_id: UUID) -> RefundResult:
        operation = await refund_execution.execute_approved_refund(
            session, env.payments, proposal_id, now=clock()
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
        propose_refund=propose_refund,
        load_approval_decision=load_approval_decision,
        execute_refund=execute_refund,
        max_clarification_rounds=settings.max_clarification_rounds,
        mode=run.mode,
    )


async def _mark_failed(session: AsyncSession, run: WorkflowRun, now: datetime) -> None:
    """Record that the run crashed, in a fresh transaction, so it never stays open."""
    await session.rollback()
    await session.refresh(run)  # rollback expired it; reload before changing it
    await set_run_status(
        session,
        run,
        RunStatus.FAILED,
        now=now,
        actor="system",
        details={"error_codes": ["INTERNAL_ERROR"]},
    )
    await session.commit()


async def get_run(session: AsyncSession, principal: Principal, run_id: UUID) -> RunResponse:
    run = await workflow_runs.get(session, run_id)
    record = None if run is None else await tickets.get_by_id(session, run.ticket_id)
    if run is None or record is None:
        raise run_not_found()
    owns_it = principal.role == Role.CUSTOMER and record.ticket.customer_id == principal.id
    is_staff = principal.role in (Role.REVIEWER, Role.ADMIN)
    if not (owns_it or is_staff):
        raise run_not_found()  # same answer as "no such run"
    return _to_response(run, record.ticket)
