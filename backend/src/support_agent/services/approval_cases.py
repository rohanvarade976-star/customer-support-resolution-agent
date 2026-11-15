"""The reviewer's view of one approval: the case behind it (ADR-019). Reviewers only.

Everything shown comes from stored rows or is computed by code now:

- the proposal as stored (its amount came from the eligibility rules, and its hash is what
  the approval approves);
- the order's facts and the eligibility rules applied to them at this moment;
- the refund policy version in force on the order's date;
- the policy sections the run cited, kept only if they exist in our policy table and shown
  with our own text (the model chose which sections, not what they say);
- the refund operation's status once one exists, so the reviewer sees what really happened;
- the readiness verdict, computed by code from all of the above and the audit trail (ADR-022);
- whether a reviewer proposed it by hand, and whether that reviewer is the caller, who then
  may not decide it (separation of duties).

No model text is returned. The complaint is the customer's own words, shown as plain text.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.repositories import policies, proposals, refunds
from support_agent.schemas.approvals import (
    ApprovalCaseResponse,
    CaseProposal,
    GoverningPolicy,
    PolicyCitation,
    ReadinessView,
    RefundSummary,
)
from support_agent.schemas.auth import Principal
from support_agent.services import run_analysis
from support_agent.services.case_readiness import assess_approval, case_facts, load_order_or_none
from support_agent.services.eligibility import LoadedOrder
from support_agent.workflow.graph import Checkpointer, cited_policy_keys


def approval_not_found() -> AppError:
    return AppError(404, "APPROVAL_NOT_FOUND", "Approval not found.")


def governing_policy(loaded: LoadedOrder) -> GoverningPolicy | None:
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
    session: AsyncSession,
    checkpointer: Checkpointer,
    settings: Settings,
    reviewer: Principal,
    approval_id: UUID,
    now: datetime,
) -> ApprovalCaseResponse:
    """The caller must already be a reviewer (the route's dependency checks it)."""
    record = await proposals.get_approval_case(session, approval_id)
    if record is None:
        raise approval_not_found()
    approval, proposal, ticket = record.approval, record.proposal, record.ticket
    payload = proposal.payload

    # Scoped to the ticket's owner, like every other order read.
    loaded = await load_order_or_none(session, ticket.customer_id, payload["order_number"])
    signals = (await run_analysis.queue_signals(session, settings, [proposal.run_id]))[
        proposal.run_id
    ]
    readiness = assess_approval(approval, proposal, loaded, signals, now)

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
        facts=None if loaded is None else case_facts(loaded, now),
        governing_policy=None if loaded is None else governing_policy(loaded),
        citations=await _citations(session, checkpointer, record.run.thread_id),
        refund=None
        if operation is None
        else RefundSummary(
            refund_id=operation.id,
            status=operation.status,
            amount_minor=operation.amount_minor,
            provider_reference=operation.provider_reference,
        ),
        readiness=ReadinessView.of(readiness),
        ai_action=signals.ai_action,
        risk_flags=signals.risk_flags,
        proposed_by_reviewer=proposal.proposed_by is not None,
        proposed_by_you=proposal.proposed_by == reviewer.id,
    )
