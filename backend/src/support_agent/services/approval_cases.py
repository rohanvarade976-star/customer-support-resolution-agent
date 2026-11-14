"""The reviewer's view of one approval: the case behind it (ADR-019). Reviewers only.

Everything shown comes from stored rows or is computed by code now:

- the proposal as stored (its amount came from the eligibility rules, and its hash is what
  the approval approves);
- the order's facts and the eligibility rules applied to them at this moment;
- the refund policy version in force on the order's date;
- the policy sections the run cited, kept only if they exist in our policy table and shown
  with our own text (the model chose which sections, not what they say);
- the refund operation's status once one exists, so the reviewer sees what really happened.

No model text is returned. The complaint is the customer's own words, shown as plain text.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.errors import AppError
from support_agent.domain.eligibility import check_eligibility
from support_agent.repositories import policies, proposals, refunds
from support_agent.schemas.approvals import (
    ApprovalCaseResponse,
    CaseFacts,
    CaseProposal,
    GoverningPolicy,
    PolicyCitation,
    RefundSummary,
)
from support_agent.services import eligibility
from support_agent.services.eligibility import LoadedOrder
from support_agent.workflow.graph import Checkpointer, cited_policy_keys


def approval_not_found() -> AppError:
    return AppError(404, "APPROVAL_NOT_FOUND", "Approval not found.")


def _facts(loaded: LoadedOrder, now: datetime) -> CaseFacts:
    facts = loaded.facts
    decision = check_eligibility(facts, now)
    return CaseFacts(
        order_status=facts.order_status,
        delivered_at=facts.delivered_at,
        return_status=facts.return_status,
        return_requested_at=facts.return_requested_at,
        return_received_at=facts.return_received_at,
        inspection_status=facts.inspection_status,
        paid_amount_minor=facts.paid_amount_minor,
        succeeded_refunds_minor=facts.succeeded_refunds_minor,
        in_progress_refunds_minor=facts.in_progress_refunds_minor,
        refundable_balance_minor=facts.balance_minor,
        refund_window_days=facts.refund_window_days,
        eligible_now=decision.eligible,
        reasons_now=decision.reasons,
    )


def _governing_policy(loaded: LoadedOrder) -> GoverningPolicy | None:
    document = loaded.refund_policy
    if document is None:
        return None
    return GoverningPolicy(
        policy_key=document.policy_key,
        version=document.version,
        title=document.title,
        refund_window_days=document.refund_window_days,
    )


async def _citations(
    session: AsyncSession, checkpointer: Checkpointer, thread_id: str
) -> list[PolicyCitation]:
    keys = await cited_policy_keys(checkpointer, thread_id)
    return [
        PolicyCitation(
            citation=chunk.chunk_key,
            policy_title=document.title,
            version=document.version,
            section_title=chunk.section_title,
            text=chunk.content,
        )
        for chunk, document in await policies.get_chunks_by_keys(session, keys)
    ]


async def get_case(
    session: AsyncSession, checkpointer: Checkpointer, approval_id: UUID, now: datetime
) -> ApprovalCaseResponse:
    """The caller must already be a reviewer (the route's dependency checks it)."""
    record = await proposals.get_approval_case(session, approval_id)
    if record is None:
        raise approval_not_found()
    approval, proposal, ticket = record.approval, record.proposal, record.ticket
    payload = proposal.payload

    loaded: LoadedOrder | None
    try:
        # Scoped to the ticket's owner, like every other order read.
        loaded = await eligibility.load_order(session, ticket.customer_id, payload["order_number"])
    except AppError:
        loaded = None

    operation = await refunds.get_for_proposal(session, proposal.id)
    return ApprovalCaseResponse(
        approval_id=approval.id,
        proposal_id=proposal.id,
        run_id=record.run.id,
        ticket_id=ticket.id,
        approval_status=approval.decision,
        created_at=approval.created_at,
        expires_at=approval.expires_at,
        decided_at=approval.decided_at,
        customer_name=record.customer_name,
        complaint=ticket.summary,
        ticket_status=ticket.status,
        proposal=CaseProposal(
            order_number=payload["order_number"],
            amount_minor=payload["amount_minor"],
            currency=payload["currency"],
            reason=payload["reason"],
            checks_passed=payload["checks_passed"],
            payload_hash=proposal.payload_hash,
            status=proposal.status,
        ),
        facts=None if loaded is None else _facts(loaded, now),
        governing_policy=None if loaded is None else _governing_policy(loaded),
        citations=await _citations(session, checkpointer, record.run.thread_id),
        refund=None
        if operation is None
        else RefundSummary(
            refund_id=operation.id,
            status=operation.status,
            amount_minor=operation.amount_minor,
            provider_reference=operation.provider_reference,
        ),
    )
