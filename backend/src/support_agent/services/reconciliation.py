"""Settle refunds whose outcome we do not know yet (FR-18, FR-19, ADR-004, ADR-014).

The worker's scheduled `reconcile_refunds` sweep calls `reconcile_due_refunds`. For each
refund operation that is due a check:

    submitted / processing      -> get_refund(provider_reference); adopt a newer status
    pending_reconciliation      -> find_refund_by_key(idempotency_key):
                                     found      -> adopt the provider's status
                                     404        -> re-send once, same key and same body
                                     other error-> no answer this time
    reserved, past the stale    -> the executor died before or while calling the provider:
    threshold                      handled exactly like pending_reconciliation

Each check is three short steps, and no transaction is open during a provider call
(CLAUDE.md rule 7):

1. Claim (repositories/refunds.py): one locked, committed UPDATE that adds one to
   `reconcile_checks` and pushes `next_reconcile_at` to the end of a lease.
2. Ask the provider (and maybe re-send). The re-send is recorded and committed BEFORE it is
   sent, so it can happen at most once per operation, whatever crashes.
3. Settle, in one transaction: lock the row only if `reconcile_checks` and the status are
   unchanged since the claim (the fencing token), then record what was verified.

On a verified answer: the operation, the proposal, the run and ticket statuses (resolved
only on `succeeded`), the customer's reply, memory and audit events change together. A run
that was already escalated stays with the person who owns it: its money records, proposal,
audit trail and memory are settled, but its status, ticket and reply are left alone.

No answer: the next check waits longer (exponential backoff with jitter). After
`reconcile_max_checks`, or at once for a definite problem more checks cannot fix, the run
is escalated with a reason code and the refund's status is left exactly as it was: we
never claim money moved, or did not move, without the provider saying so.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agents.resolve import ESCALATION_MESSAGE
from support_agent.core.config import Settings
from support_agent.core.tracing import span
from support_agent.db.models import ActionProposal, RefundOperation, Ticket, WorkflowRun
from support_agent.domain.enums import (
    ApprovalDecision,
    MemoryKind,
    MemoryOutcome,
    ProposalStatus,
    RefundOperationStatus,
    RunStatus,
    TicketStatus,
)
from support_agent.domain.memory import refund_memory_outcome
from support_agent.domain.proposals import RefundProposalPayload, payload_hash
from support_agent.domain.refund_outcomes import (
    ACCEPTED_STATUSES,
    FINAL_STATUSES,
    status_to_adopt,
    statuses_after_refund,
)
from support_agent.integrations.errors import IntegrationError, ResourceNotFoundError
from support_agent.integrations.payments import PaymentClient
from support_agent.repositories import audit, proposals, refunds, tickets
from support_agent.repositories.refunds import ReconcileClaim
from support_agent.services import memory, refund_execution
from support_agent.services.case_updates import load_case, set_run_status, set_ticket_status
from support_agent.worker.queue import backoff_delay

log = logging.getLogger(__name__)

ACTOR = "reconciler"
EXHAUSTED = "RECONCILIATION_EXHAUSTED"


@dataclass(frozen=True)
class Finding:
    """What one check learned. `reported` None: no clear answer this time."""

    reported: RefundOperationStatus | None = None
    provider_reference: str | None = None
    error_code: str | None = None
    give_up_now: bool = False  # a definite problem that more checks cannot fix
    resent: bool = False


async def reconcile_due_refunds(
    session: AsyncSession,
    payments: PaymentClient,
    settings: Settings,
    clock: Callable[[], datetime],
) -> int:
    """The sweep: check up to `reconcile_batch_size` due operations. Returns how many.

    Each operation at most once per sweep, even if its next check is already due again
    (a very short backoff): its checks are meant to be spread out over time.
    """
    checked: set[UUID] = set()
    with span("reconcile.sweep") as sweep_span:
        while len(checked) < settings.reconcile_batch_size:
            claim = await refunds.claim_for_reconciliation(
                session,
                now=clock(),
                lease_seconds=settings.reconcile_lease_seconds,
                skip=checked,
            )
            if claim is None:
                break
            with span("reconcile.check", {"reconcile.refund_id": str(claim.id)}) as check_span:
                finding = await check_claim(session, payments, claim, clock())
                check_span.set(
                    "reconcile.reported", finding.reported.value if finding.reported else "none"
                )
                if finding.error_code is not None:
                    check_span.set("error.code", finding.error_code)
            await settle_claim(session, settings, claim, finding, clock())
            checked.add(claim.id)
        sweep_span.set("reconcile.checked", len(checked))
    return len(checked)


# ---------------------------------------------------------------- 2. ask the provider


async def check_claim(
    session: AsyncSession, payments: PaymentClient, claim: ReconcileClaim, now: datetime
) -> Finding:
    """Step 2: ask the provider about a claimed operation (and maybe re-send). No lock held."""
    try:
        if claim.provider_reference is not None:
            refund = await payments.get_refund(claim.provider_reference)
        else:  # reserved or pending_reconciliation: we never learned the provider's id
            refund = await payments.find_refund_by_key(claim.idempotency_key)
    except ResourceNotFoundError:
        if claim.provider_reference is not None:
            # The provider once confirmed this refund and now has no record of it.
            return Finding(error_code="CONFIRMED_REFUND_MISSING", give_up_now=True)
        # A definite "no refund was created with this key" (ADR-004).
        return await _resend_once(session, payments, claim, now)
    except IntegrationError as exc:  # timeout, 5xx, ...: no answer, check again later
        return Finding(error_code=exc.error_code)
    except ValidationError:
        return Finding(error_code="UNREADABLE_RESPONSE")

    # Never adopt a refund that is not ours: same key, same amount.
    if refund.idempotency_key != claim.idempotency_key or refund.amount_minor != (
        claim.amount_minor
    ):
        return Finding(error_code="PROVIDER_MISMATCH", give_up_now=True)
    return Finding(refund.status, refund.provider_reference)


async def _resend_once(
    session: AsyncSession, payments: PaymentClient, claim: ReconcileClaim, now: datetime
) -> Finding:
    if claim.resent:
        # Re-sent once already and the provider still has nothing: never a third request.
        return Finding(error_code="NOT_FOUND_AFTER_RESEND")
    payload = await _approved_payload(session, claim)
    if payload is None:
        return Finding(error_code="PROPOSAL_CHANGED", give_up_now=True)
    if not await refunds.mark_resent(session, claim, now=now):
        # Another reconciler or the executor got there first; settling will write nothing.
        return Finding(error_code="CLAIM_LOST")
    outcome = await refund_execution.send_refund(
        payments,
        idempotency_key=claim.idempotency_key,
        amount_minor=claim.amount_minor,
        payload=payload,
    )
    return Finding(outcome.status, outcome.provider_reference, outcome.error_code, resent=True)


async def _approved_payload(
    session: AsyncSession, claim: ReconcileClaim
) -> RefundProposalPayload | None:
    """The approved request, to send again exactly as it was; None if it no longer matches.

    The approval's expiry is not checked again: it was valid when the amount was reserved,
    and the re-send completes that same reserved operation (same key), not a new refund.
    """
    proposal = await proposals.get_proposal(session, claim.proposal_id)
    approval = await proposals.get_approval_for_proposal(session, claim.proposal_id)
    if (
        proposal is None
        or approval is None
        or approval.decision != ApprovalDecision.APPROVED
        or payload_hash(proposal.payload) != approval.payload_hash
    ):
        return None
    try:
        payload = RefundProposalPayload.model_validate(proposal.payload)
    except ValidationError:
        return None
    return payload if payload.amount_minor == claim.amount_minor else None


# ---------------------------------------------------------------- 3. settle


async def settle_claim(
    session: AsyncSession,
    settings: Settings,
    claim: ReconcileClaim,
    finding: Finding,
    now: datetime,
) -> None:
    """Step 3: record what the check learned, in one short transaction. Commits.

    Writes nothing if the claim no longer holds (see repositories/refunds.lock_if_held).
    """
    operation = await refunds.lock_if_held(session, claim)
    if operation is None:
        await session.commit()
        log.info("reconcile_claim_lost", extra={"refund_id": str(claim.id), "check": claim.check})
        return

    proposal, run, ticket = await load_case(session, operation.proposal_id)
    if finding.resent:
        await audit.record(
            session,
            run_id=run.id,
            actor=ACTOR,
            event_type="refund.resent",
            payload={"refund_id": str(operation.id), "idempotency_key": claim.idempotency_key},
        )

    adopted = status_to_adopt(operation.status, finding.reported)
    if adopted is not None:
        await _adopt(session, settings, operation, proposal, run, ticket, adopted, finding, now)
    elif finding.give_up_now or operation.reconcile_checks >= settings.reconcile_max_checks:
        await _escalate(session, settings, operation, run, ticket, finding, now)
    else:
        operation.last_reconcile_error = finding.error_code
        operation.next_reconcile_at = _next_check(settings, operation, now)
    await session.commit()
    log.info(
        "refund_checked",
        extra={
            "run_id": str(run.id),
            "refund_id": str(operation.id),
            "check": claim.check,
            "status": operation.status.value,
            "error_code": finding.error_code,
        },
    )


def _next_check(settings: Settings, operation: RefundOperation, now: datetime) -> datetime:
    delay = backoff_delay(
        operation.reconcile_checks,
        base_seconds=settings.reconcile_backoff_base_seconds,
        max_seconds=settings.reconcile_backoff_max_seconds,
    )
    return now + timedelta(seconds=delay)


async def _adopt(
    session: AsyncSession,
    settings: Settings,
    operation: RefundOperation,
    proposal: ActionProposal,
    run: WorkflowRun,
    ticket: Ticket,
    status: RefundOperationStatus,
    finding: Finding,
    now: datetime,
) -> None:
    """The provider verified a newer status: record it everywhere it shows."""
    previous = operation.status
    operation.status = status
    if finding.provider_reference is not None:
        operation.provider_reference = finding.provider_reference
    operation.last_reconcile_error = None
    final = status in FINAL_STATUSES
    operation.next_reconcile_at = None if final else _next_check(settings, operation, now)
    await session.flush()

    await audit.record(
        session,
        run_id=run.id,
        actor=ACTOR,
        event_type="refund.reconciled",
        payload={
            "refund_id": str(operation.id),
            "from_status": previous.value,
            "status": status.value,
            "provider_reference": operation.provider_reference,
            "checks": operation.reconcile_checks,
            "resent": operation.resent_at is not None,
        },
    )
    await audit.record(  # the same event the executor writes for this status
        session,
        run_id=run.id,
        actor=ACTOR,
        event_type=refund_execution.EVENT_FOR_STATUS[status],
        payload={
            "refund_id": str(operation.id),
            "status": status.value,
            "provider_reference": operation.provider_reference,
            "error_code": finding.error_code,
        },
    )
    if status in ACCEPTED_STATUSES and proposal.status != ProposalStatus.EXECUTED:
        await proposals.set_proposal_status(session, proposal, ProposalStatus.EXECUTED)

    if run.status != RunStatus.ESCALATED:  # an escalated case belongs to a person now
        run_status, ticket_status = statuses_after_refund(status)
        await set_run_status(session, run, run_status, now=now, actor=ACTOR)
        await set_ticket_status(session, ticket, ticket_status, run_id=run.id, actor=ACTOR)
        await tickets.set_reply(session, ticket, refund_execution.describe(status))

    outcome = refund_memory_outcome(status)
    if outcome is not None:
        await memory.record_refund_outcome(
            session,
            settings,
            run=run,
            ticket=ticket,
            order_id=operation.order_id,
            outcome=outcome,
            now=now,
        )


async def _escalate(
    session: AsyncSession,
    settings: Settings,
    operation: RefundOperation,
    run: WorkflowRun,
    ticket: Ticket,
    finding: Finding,
    now: datetime,
) -> None:
    """Stop checking and hand the case to a person. The refund's status is not touched."""
    code = finding.error_code if finding.give_up_now else EXHAUSTED
    operation.next_reconcile_at = None  # never picked up again
    operation.last_reconcile_error = finding.error_code
    await session.flush()
    await audit.record(
        session,
        run_id=run.id,
        actor=ACTOR,
        event_type="refund.reconciliation_stopped",
        payload={
            "refund_id": str(operation.id),
            "status": operation.status.value,  # unchanged: still unknown or in progress
            "reason": code,
            "checks": operation.reconcile_checks,
            "error_code": finding.error_code,
        },
    )
    if run.status == RunStatus.ESCALATED:
        return  # already with a person; the event above tells them why checks stopped
    await set_run_status(
        session,
        run,
        RunStatus.ESCALATED,
        now=now,
        actor=ACTOR,
        details={"reason": code, "refund_id": str(operation.id), "error_code": finding.error_code},
    )
    await set_ticket_status(session, ticket, TicketStatus.ESCALATED, run_id=run.id, actor=ACTOR)
    await tickets.set_reply(session, ticket, ESCALATION_MESSAGE)
    await memory.record_run_outcome(
        session,
        settings,
        run=run,
        ticket=ticket,
        kind=MemoryKind.CASE_ESCALATED,
        outcome=MemoryOutcome.ESCALATED,
        order_number=None,  # the ticket's own order
        now=now,
    )
