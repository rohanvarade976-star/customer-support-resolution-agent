"""The reviewer's approval queue and decisions (FR-14 to FR-17).

The reviewer's identity comes from the token (the Principal), never from the request
body. A decision is refused when the approval was already decided or its proposal is no
longer pending. An expired approval, or a proposal that no longer hashes to the approved
value, is recorded as such and never executes.

Two kinds of run reach this endpoint:

- Manual (M3) runs, started by a reviewer: unchanged. This service records the decision,
  updates the run and ticket, and (on approval) calls the refund executor itself.
- Workflow runs, paused in `wait_for_approval`. In one transaction, while the approval row
  is locked, this service claims the run (awaiting_approval -> executing; a second decision
  or a finished run gets 409), stores the decision and queues a `resume_after_decision`
  job. It commits and answers 202: the worker resumes the graph (M7 Task 2). After the
  resume everything is fixed code that reads the STORED decision: approve -> revalidate and
  execute; reject -> reopen; expired or changed -> escalate. The worker gives that job a
  disabled LLM client, so no model can run there.

The worker also sweeps approvals that expired with no decision (`expire_overdue_approvals`):
each is recorded as expired and its paused run is claimed and queued the same way, so the
graph escalates it.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.errors import AppError
from support_agent.db.models import ActionProposal, Approval, RefundOperation, Ticket, WorkflowRun
from support_agent.domain.enums import (
    ApprovalDecision,
    JobType,
    ProposalStatus,
    RunMode,
    RunStatus,
    TicketStatus,
)
from support_agent.domain.proposals import payload_hash
from support_agent.integrations.payments import PaymentClient
from support_agent.repositories import audit, proposals
from support_agent.schemas.approvals import (
    ApprovalListResponse,
    DecisionRequest,
    DecisionResponse,
    PendingApproval,
    RefundSummary,
)
from support_agent.schemas.auth import Principal
from support_agent.services import refund_execution
from support_agent.services import runs as runs_service
from support_agent.services.case_updates import load_case, set_run_status, set_ticket_status

ACTOR = "reviewer"
REJECTED_MESSAGE = "The refund proposal was rejected. The ticket is open again for follow-up."
# Workflow runs: the decision is stored, but its effects have not happened yet.
APPROVED_QUEUED_MESSAGE = (
    "The approval was recorded. The refund is being processed; the run shows the outcome."
)
REJECTED_QUEUED_MESSAGE = "The rejection was recorded. The case is being updated."


@dataclass(frozen=True)
class DecisionOutcome:
    """The response, and whether its effects were queued (202) or already done (200)."""

    response: DecisionResponse
    queued: bool


def _expired_error() -> AppError:
    return AppError(
        409, "APPROVAL_EXPIRED", "This approval has expired. Create a new proposal to continue."
    )


def _changed_error() -> AppError:
    return AppError(
        409, "PROPOSAL_CHANGED", "The proposal no longer matches what was submitted for review."
    )


async def list_pending(session: AsyncSession, now: datetime) -> ApprovalListResponse:
    items = [
        PendingApproval(
            approval_id=record.approval.id,
            proposal_id=record.proposal.id,
            ticket_id=record.ticket_id,
            order_number=record.order_number,
            customer_name=record.customer_name,
            amount_minor=record.proposal.payload["amount_minor"],
            currency=record.proposal.payload["currency"],
            reason=record.proposal.payload["reason"],
            reasons=record.proposal.payload["checks_passed"],
            created_at=record.approval.created_at,
            expires_at=record.approval.expires_at,
        )
        for record in await proposals.list_pending(session, now)
    ]
    return ApprovalListResponse(items=items)


async def decide(
    session: AsyncSession,
    reviewer: Principal,
    approval_id: UUID,
    body: DecisionRequest,
    env: runs_service.EnqueueEnv,
    payments: PaymentClient,
) -> DecisionOutcome:
    now = env.clock()
    # Locked until the decision is committed, so two reviewers cannot both decide.
    approval = await proposals.lock_approval(session, approval_id)
    if approval is None:
        raise AppError(404, "APPROVAL_NOT_FOUND", "Approval not found.")
    proposal, run, ticket = await load_case(session, approval.proposal_id)

    if approval.decision != ApprovalDecision.PENDING:
        raise AppError(409, "ALREADY_DECIDED", "This approval has already been decided.")
    if proposal.status != ProposalStatus.PENDING_APPROVAL:
        raise AppError(409, "PROPOSAL_NOT_PENDING", "This proposal is no longer awaiting review.")

    if run.mode == RunMode.MANUAL:
        response = await _decide_manual(
            session, payments, approval, proposal, run, ticket, reviewer, body, now
        )
        return DecisionOutcome(response, queued=False)
    response = await _decide_and_queue(session, env, approval, proposal, run, reviewer, body, now)
    return DecisionOutcome(response, queued=True)


# ---------------------------------------------------------------- workflow runs


async def _decide_and_queue(
    session: AsyncSession,
    env: runs_service.EnqueueEnv,
    approval: Approval,
    proposal: ActionProposal,
    run: WorkflowRun,
    reviewer: Principal,
    body: DecisionRequest,
    now: datetime,
) -> DecisionResponse:
    await runs_service.claim_paused_run(
        session,
        run,
        waiting=RunStatus.AWAITING_APPROVAL,
        resuming=RunStatus.EXECUTING,
        resumed_by=ACTOR,
        actor_id=reviewer.id,
    )
    expired = now >= approval.expires_at
    changed = not expired and payload_hash(proposal.payload) != approval.payload_hash
    reviewer_id = str(reviewer.id)
    if expired:
        await _record_expired(session, approval, proposal, run.id, now)
    elif changed:
        await _record_changed(session, proposal, run.id)
    elif body.decision == "reject":
        await _record_rejected(session, approval, proposal, run.id, reviewer_id, body.note, now)
    else:
        await _record_approved(session, approval, proposal, run.id, reviewer_id, body.note, now)
    # Every outcome resumes the run: the graph escalates an expired or changed proposal.
    await _queue_resume(session, env, run, approval)
    await session.commit()  # decision, claim and job together; the approval lock is released

    if expired:
        raise _expired_error()
    if changed:
        raise _changed_error()
    rejected = body.decision == "reject"
    return DecisionResponse(
        approval_id=approval.id,
        proposal_id=proposal.id,
        decision="rejected" if rejected else "approved",
        proposal_status=proposal.status,
        refund=None,  # the refund (if any) is the worker's; GET /runs/{id} shows the outcome
        message=REJECTED_QUEUED_MESSAGE if rejected else APPROVED_QUEUED_MESSAGE,
    )


async def _queue_resume(
    session: AsyncSession, env: runs_service.EnqueueEnv, run: WorkflowRun, approval: Approval
) -> None:
    await runs_service.enqueue_resume(
        session, env, run, JobType.RESUME_AFTER_DECISION, {"approval_id": str(approval.id)}
    )


async def expire_overdue_approvals(session: AsyncSession, env: runs_service.EnqueueEnv) -> int:
    """Expire every undecided approval past its time on a workflow run. Returns how many.

    One approval per transaction: lock it (skipping any a reviewer holds right now), record
    the expiry, claim its paused run and queue the resume, commit. The graph then reads the
    stored "expired" decision and escalates. An expired approval is never selected again,
    so the loop ends.
    """
    expired = 0
    while True:
        now = env.clock()
        approval = await proposals.lock_next_expired_workflow_approval(session, now)
        if approval is None:
            await session.commit()
            return expired
        proposal, run, _ = await load_case(session, approval.proposal_id)
        await _record_expired(session, approval, proposal, run.id, now)
        if run.status == RunStatus.AWAITING_APPROVAL:
            await runs_service.claim_paused_run(
                session,
                run,
                waiting=RunStatus.AWAITING_APPROVAL,
                resuming=RunStatus.EXECUTING,
                resumed_by="system",
                actor_id=None,
            )
            await _queue_resume(session, env, run, approval)
        await session.commit()
        expired += 1


# ---------------------------------------------------------------- manual (M3) runs


async def _decide_manual(
    session: AsyncSession,
    payments: PaymentClient,
    approval: Approval,
    proposal: ActionProposal,
    run: WorkflowRun,
    ticket: Ticket,
    reviewer: Principal,
    body: DecisionRequest,
    now: datetime,
) -> DecisionResponse:
    if now >= approval.expires_at:
        await _expire_manual(session, approval, proposal, run, ticket, now)
    if payload_hash(proposal.payload) != approval.payload_hash:
        await _refuse_changed_manual(session, proposal, run, ticket, now)

    reviewer_id = str(reviewer.id)
    if body.decision == "reject":
        await _record_rejected(session, approval, proposal, run.id, reviewer_id, body.note, now)
        await set_run_status(session, run, RunStatus.COMPLETED, now=now, actor=ACTOR)
        await set_ticket_status(session, ticket, TicketStatus.OPEN, run_id=run.id, actor=ACTOR)
        await session.commit()
        return _rejected_response(approval, proposal)

    await _record_approved(session, approval, proposal, run.id, reviewer_id, body.note, now)
    await set_run_status(session, run, RunStatus.EXECUTING, now=now, actor=ACTOR)
    await session.commit()  # the decision is stored (and the lock released) before money moves

    operation = await refund_execution.execute_approved_refund(
        session, payments, proposal.id, now=now
    )
    return _approved_response(approval, proposal, operation)


async def _expire_manual(
    session: AsyncSession,
    approval: Approval,
    proposal: ActionProposal,
    run: WorkflowRun,
    ticket: Ticket,
    now: datetime,
) -> NoReturn:
    """Too late to decide: record the expiry, reopen the ticket, then refuse."""
    await _record_expired(session, approval, proposal, run.id, now)
    await set_run_status(session, run, RunStatus.ESCALATED, now=now, actor="system")
    await set_ticket_status(session, ticket, TicketStatus.OPEN, run_id=run.id, actor="system")
    await session.commit()
    raise _expired_error()


async def _refuse_changed_manual(
    session: AsyncSession,
    proposal: ActionProposal,
    run: WorkflowRun,
    ticket: Ticket,
    now: datetime,
) -> NoReturn:
    """The stored proposal no longer matches its hash: someone changed it. Never approve."""
    await _record_changed(session, proposal, run.id)
    await set_run_status(session, run, RunStatus.ESCALATED, now=now, actor="system")
    await set_ticket_status(session, ticket, TicketStatus.ESCALATED, run_id=run.id, actor="system")
    await session.commit()
    raise _changed_error()


# ---------------------------------------------------------------- recording a decision
# Shared by both kinds of run. Each changes the approval and proposal and writes the event;
# nothing here touches the run or the ticket, and nothing is committed.


def _decision_payload(approval: Approval, reviewer_id: str, note: str | None) -> dict[str, Any]:
    return {
        "approval_id": str(approval.id),
        "proposal_id": str(approval.proposal_id),
        "reviewer_id": reviewer_id,
        "note": note,
    }


async def _record_approved(
    session: AsyncSession,
    approval: Approval,
    proposal: ActionProposal,
    run_id: UUID,
    reviewer_id: str,
    note: str | None,
    now: datetime,
) -> None:
    await proposals.record_decision(
        session,
        approval,
        decision=ApprovalDecision.APPROVED,
        reviewer_id=reviewer_id,
        decided_at=now,
    )
    await proposals.set_proposal_status(session, proposal, ProposalStatus.APPROVED)
    await audit.record(
        session,
        run_id=run_id,
        actor=ACTOR,
        event_type="approval.approved",
        payload=_decision_payload(approval, reviewer_id, note),
    )


async def _record_rejected(
    session: AsyncSession,
    approval: Approval,
    proposal: ActionProposal,
    run_id: UUID,
    reviewer_id: str,
    note: str | None,
    now: datetime,
) -> None:
    await proposals.record_decision(
        session,
        approval,
        decision=ApprovalDecision.REJECTED,
        reviewer_id=reviewer_id,
        decided_at=now,
    )
    await proposals.set_proposal_status(session, proposal, ProposalStatus.REJECTED)
    await audit.record(
        session,
        run_id=run_id,
        actor=ACTOR,
        event_type="approval.rejected",
        payload=_decision_payload(approval, reviewer_id, note),
    )


async def _record_expired(
    session: AsyncSession,
    approval: Approval,
    proposal: ActionProposal,
    run_id: UUID,
    now: datetime,
) -> None:
    await proposals.record_decision(
        session, approval, decision=ApprovalDecision.EXPIRED, reviewer_id=None, decided_at=now
    )
    await proposals.set_proposal_status(session, proposal, ProposalStatus.EXPIRED)
    await audit.record(
        session,
        run_id=run_id,
        actor="system",
        event_type="approval.expired",
        payload={"approval_id": str(approval.id), "proposal_id": str(proposal.id)},
    )


async def _record_changed(session: AsyncSession, proposal: ActionProposal, run_id: UUID) -> None:
    await proposals.set_proposal_status(session, proposal, ProposalStatus.INVALIDATED)
    await audit.record(
        session,
        run_id=run_id,
        actor="system",
        event_type="validation.failed",
        payload={"proposal_id": str(proposal.id), "code": "PROPOSAL_CHANGED"},
    )


# ---------------------------------------------------------------- responses


def _rejected_response(approval: Approval, proposal: ActionProposal) -> DecisionResponse:
    return DecisionResponse(
        approval_id=approval.id,
        proposal_id=proposal.id,
        decision="rejected",
        proposal_status=proposal.status,
        refund=None,
        message=REJECTED_MESSAGE,
    )


def _approved_response(
    approval: Approval, proposal: ActionProposal, operation: RefundOperation
) -> DecisionResponse:
    return DecisionResponse(
        approval_id=approval.id,
        proposal_id=proposal.id,
        decision="approved",
        proposal_status=proposal.status,
        refund=RefundSummary(
            refund_id=operation.id,
            status=operation.status,
            amount_minor=operation.amount_minor,
            provider_reference=operation.provider_reference,
        ),
        message=refund_execution.describe(operation.status),
    )
