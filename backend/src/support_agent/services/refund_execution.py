"""Issue an approved refund exactly once (FR-16, FR-17, ADR-004).

Only code calls this, after a stored approval; no agent can reach it. Three steps:

1. Revalidate: the approval is approved and unexpired, the payload hash still matches,
   and fresh eligibility still allows the amount. Any change invalidates the proposal.
2. Reserve, in one short transaction: lock the order row (SELECT ... FOR UPDATE),
   recheck the balance, insert a "reserved" refund operation, commit. The lock is
   gone before step 3.
3. Call the payment provider exactly once with a stable idempotency key, then record
   what it said. An unclear answer becomes pending_reconciliation, never "success".

Calling this again for the same proposal returns the existing refund operation
without calling the provider again (and the database allows only one per proposal).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.db.models import ActionProposal, RefundOperation, Ticket, WorkflowRun
from support_agent.domain.eligibility import check_eligibility
from support_agent.domain.enums import (
    ApprovalDecision,
    ProposalStatus,
    RefundOperationStatus,
    RunStatus,
    TicketStatus,
)
from support_agent.domain.money import refund_totals, refundable_balance
from support_agent.domain.proposals import RefundProposalPayload, payload_hash
from support_agent.integrations.errors import (
    OutcomeUnknownError,
    ProviderRejectedError,
    ProviderUnavailableError,
    ResourceNotFoundError,
)
from support_agent.integrations.payments import PaymentClient
from support_agent.repositories import audit, orders, proposals, refunds
from support_agent.services import eligibility
from support_agent.services.case_updates import load_case, set_run_status, set_ticket_status

ACTOR = "system"

# The provider took the refund on. Only SUCCEEDED means the money has arrived.
ACCEPTED_STATUSES = frozenset(
    {
        RefundOperationStatus.SUBMITTED,
        RefundOperationStatus.PROCESSING,
        RefundOperationStatus.SUCCEEDED,
    }
)

_EVENT_FOR_STATUS = {
    RefundOperationStatus.SUBMITTED: "refund.submitted",
    RefundOperationStatus.PROCESSING: "refund.processing",
    RefundOperationStatus.SUCCEEDED: "refund.succeeded",
    RefundOperationStatus.FAILED: "refund.failed",
    RefundOperationStatus.PENDING_RECONCILIATION: "refund.reconciling",
}

# What we may say about a refund in each state. Never more than the provider confirmed.
_MESSAGES = {
    RefundOperationStatus.RESERVED: "The refund is reserved but has not reached the payment "
    "service yet. The case is being checked.",
    RefundOperationStatus.SUBMITTED: "The payment service accepted the refund request. "
    "Processing is still pending.",
    RefundOperationStatus.PROCESSING: "The payment service accepted the refund request. "
    "Processing is still pending.",
    RefundOperationStatus.SUCCEEDED: "The refund has been completed.",
    RefundOperationStatus.FAILED: "The refund could not be issued. The ticket has been "
    "escalated for follow-up.",
    RefundOperationStatus.PENDING_RECONCILIATION: "We couldn't confirm the refund outcome yet. "
    "The case is being checked.",
}


def describe(status: RefundOperationStatus) -> str:
    return _MESSAGES[status]


def idempotency_key_for(proposal_id: UUID) -> str:
    """Stable: the same proposal always gets the same key, on every attempt."""
    return f"refund-{proposal_id}"


@dataclass(frozen=True)
class _Case:
    """Everything revalidation proved, passed on to the next steps."""

    proposal: ActionProposal
    run: WorkflowRun
    ticket: Ticket
    payload: RefundProposalPayload


@dataclass(frozen=True)
class _Outcome:
    status: RefundOperationStatus
    provider_reference: str | None
    error_code: str | None


async def execute_approved_refund(
    session: AsyncSession,
    payments: PaymentClient,
    proposal_id: UUID,
    *,
    now: datetime,
    settings: Settings,
) -> RefundOperation:
    existing = await refunds.get_for_proposal(session, proposal_id)
    if existing is not None:
        return existing  # already executed once: never call the provider again

    case = await _revalidate(session, proposal_id, now, settings)
    operation, created = await _reserve(session, case, now)
    if not created:
        return operation

    outcome = await _call_provider(payments, operation, case.payload)
    await _record_outcome(session, case, operation, outcome, now)
    return operation


# ---------------------------------------------------------------- 1. revalidate


async def _revalidate(
    session: AsyncSession, proposal_id: UUID, now: datetime, settings: Settings
) -> _Case:
    proposal, run, ticket = await load_case(session, proposal_id)
    approval = await proposals.get_approval_for_proposal(session, proposal_id)
    if (
        approval is None
        or approval.decision != ApprovalDecision.APPROVED
        or proposal.status != ProposalStatus.APPROVED
    ):
        # Not an invalidation: nobody approved this, so there is nothing to execute.
        raise AppError(409, "NOT_APPROVED", "This proposal has no valid approval.")

    if now >= approval.expires_at:
        await _invalidate(session, proposal, run, ticket, "APPROVAL_EXPIRED", now)
    if payload_hash(proposal.payload) != approval.payload_hash:
        await _invalidate(session, proposal, run, ticket, "PROPOSAL_CHANGED", now)
    try:
        payload = RefundProposalPayload.model_validate(proposal.payload)
    except ValidationError:
        await _invalidate(session, proposal, run, ticket, "PROPOSAL_CHANGED", now)

    # Ownership: the proposal's customer and order must match the ticket's.
    if payload.customer_id != ticket.customer_id or payload.order_id != ticket.order_id:
        await _invalidate(session, proposal, run, ticket, "OWNERSHIP_MISMATCH", now)

    loaded = await eligibility.load_order(session, ticket.customer_id, payload.order_number)
    decision = check_eligibility(loaded.facts, now, settings.refund_window_days)
    if not decision.eligible:
        reasons = [reason.value for reason in decision.reasons]
        await _invalidate(session, proposal, run, ticket, "NO_LONGER_ELIGIBLE", now, reasons)
    if payload.amount_minor > decision.refundable_amount_minor:
        await _invalidate(session, proposal, run, ticket, "BALANCE_CHANGED", now)

    return _Case(proposal, run, ticket, payload)


async def _invalidate(
    session: AsyncSession,
    proposal: ActionProposal,
    run: WorkflowRun,
    ticket: Ticket,
    code: str,
    now: datetime,
    reasons: list[str] | None = None,
) -> NoReturn:
    """Record that the approval no longer holds, escalate, commit, then refuse."""
    await proposals.set_proposal_status(session, proposal, ProposalStatus.INVALIDATED)
    payload: dict[str, Any] = {"proposal_id": str(proposal.id), "code": code}
    if reasons:
        payload["reasons"] = reasons
    await audit.record(
        session, run_id=run.id, actor=ACTOR, event_type="validation.failed", payload=payload
    )
    await set_run_status(session, run, RunStatus.ESCALATED, now=now, actor=ACTOR)
    await set_ticket_status(session, ticket, TicketStatus.ESCALATED, run_id=run.id, actor=ACTOR)
    await session.commit()
    details: dict[str, Any] = {"code": code}
    if reasons:
        details["reasons"] = reasons
    raise AppError(
        409,
        "REVALIDATION_FAILED",
        "The approved proposal no longer matches the order. A new proposal is needed.",
        details=details,
    )


# ---------------------------------------------------------------- 2. reserve


async def _reserve(
    session: AsyncSession, case: _Case, now: datetime
) -> tuple[RefundOperation, bool]:
    """Short locked transaction. Returns (operation, created_now)."""
    order = await orders.lock_by_id(session, case.payload.order_id)  # lock held from here...
    if order is None:  # foreign keys make this impossible
        raise AppError(500, "INTERNAL_ERROR", "Order disappeared.")

    # Checked again under the lock: a parallel call may have reserved in the meantime.
    existing = await refunds.get_for_proposal(session, case.proposal.id)
    if existing is not None:
        await session.commit()
        return existing, False

    succeeded, in_progress = refund_totals(await refunds.sum_by_status(session, order.id))
    balance = refundable_balance(order.paid_amount_minor, succeeded, in_progress)
    if case.payload.amount_minor > balance:
        await _invalidate(session, case.proposal, case.run, case.ticket, "BALANCE_CHANGED", now)

    operation = await refunds.insert_reserved(
        session,
        order_id=order.id,
        proposal_id=case.proposal.id,
        amount_minor=case.payload.amount_minor,
        idempotency_key=idempotency_key_for(case.proposal.id),
    )
    await audit.record(
        session,
        run_id=case.run.id,
        actor=ACTOR,
        event_type="refund.reserved",
        payload={
            "refund_id": str(operation.id),
            "proposal_id": str(case.proposal.id),
            "amount_minor": operation.amount_minor,
            "idempotency_key": operation.idempotency_key,
        },
    )
    await session.commit()  # ...to here. No network call happened while it was held.
    return operation, True


# ---------------------------------------------------------------- 3. call and record


async def _call_provider(
    payments: PaymentClient, operation: RefundOperation, payload: RefundProposalPayload
) -> _Outcome:
    """Exactly one attempt (ADR-004). Every possible answer maps to one of our statuses."""
    try:
        refund = await payments.create_refund(
            idempotency_key=operation.idempotency_key,
            order_reference=payload.order_number,
            amount_minor=operation.amount_minor,
            currency=payload.currency,
            reason=payload.reason,
        )
    except OutcomeUnknownError as exc:
        # A refund may exist. Only reconciliation by key may decide what happened.
        return _Outcome(RefundOperationStatus.PENDING_RECONCILIATION, None, exc.error_code)
    except ProviderUnavailableError as exc:
        # create_refund only lets this through when nothing was sent; stay safe anyway.
        status = (
            RefundOperationStatus.PENDING_RECONCILIATION
            if exc.request_sent
            else RefundOperationStatus.FAILED
        )
        return _Outcome(status, None, exc.error_code)
    except (ProviderRejectedError, ResourceNotFoundError) as exc:
        return _Outcome(RefundOperationStatus.FAILED, None, exc.error_code)
    return _Outcome(refund.status, refund.provider_reference, None)


async def _record_outcome(
    session: AsyncSession,
    case: _Case,
    operation: RefundOperation,
    outcome: _Outcome,
    now: datetime,
) -> None:
    await refunds.set_outcome(session, operation, outcome.status, outcome.provider_reference)
    await audit.record(
        session,
        run_id=case.run.id,
        actor=ACTOR,
        event_type=_EVENT_FOR_STATUS[outcome.status],
        payload={
            "refund_id": str(operation.id),
            "status": outcome.status.value,
            "provider_reference": outcome.provider_reference,
            "error_code": outcome.error_code,
        },
    )

    if outcome.status in ACCEPTED_STATUSES:
        await proposals.set_proposal_status(session, case.proposal, ProposalStatus.EXECUTED)
        await set_run_status(session, case.run, RunStatus.COMPLETED, now=now, actor=ACTOR)
        # Resolved only once the money has verifiably arrived.
        ticket_status = (
            TicketStatus.RESOLVED
            if outcome.status == RefundOperationStatus.SUCCEEDED
            else TicketStatus.IN_PROGRESS
        )
    elif outcome.status == RefundOperationStatus.FAILED:
        await set_run_status(session, case.run, RunStatus.FAILED, now=now, actor=ACTOR)
        ticket_status = TicketStatus.ESCALATED
    else:  # pending_reconciliation: the run stays "executing" until the outcome is known
        ticket_status = TicketStatus.IN_PROGRESS

    await set_ticket_status(session, case.ticket, ticket_status, run_id=case.run.id, actor=ACTOR)
    await session.commit()
