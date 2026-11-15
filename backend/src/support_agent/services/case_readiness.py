"""The order's facts read now, and the readiness verdict for one approval (ADR-022).

Shared by the reviewer's case view, every row of the approval queue and the escalations
inbox, so all three show the same facts and the same verdict. Reads only; nothing is written.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.errors import AppError
from support_agent.db.models import ActionProposal, Approval
from support_agent.domain.eligibility import check_eligibility
from support_agent.domain.readiness import ReadinessInput, ReadinessResult, assess
from support_agent.schemas.approvals import CaseFacts
from support_agent.services import eligibility
from support_agent.services.eligibility import LoadedOrder
from support_agent.services.run_analysis import QueueSignals


async def load_order_or_none(
    session: AsyncSession, customer_id: UUID, order_number: str
) -> LoadedOrder | None:
    """The order, scoped to its owner like every other order read; None if it cannot be read."""
    try:
        return await eligibility.load_order(session, customer_id, order_number)
    except AppError:
        return None


def case_facts(loaded: LoadedOrder, now: datetime) -> CaseFacts:
    """The order's facts and the eligibility rules applied to them at this moment."""
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


def assess_approval(
    approval: Approval,
    proposal: ActionProposal,
    loaded: LoadedOrder | None,
    signals: QueueSignals,
    now: datetime,
) -> ReadinessResult:
    decision = None if loaded is None else check_eligibility(loaded.facts, now)
    return assess(
        ReadinessInput(
            approval_status=approval.decision,
            expires_at=approval.expires_at,
            proposal_amount_minor=proposal.payload["amount_minor"],
            proposed_by_reviewer=proposal.proposed_by is not None,
            order_readable=decision is not None,
            eligible_now=None if decision is None else decision.eligible,
            reasons_now=[] if decision is None else [r.value for r in decision.reasons],
            refundable_now_minor=None if decision is None else decision.refundable_amount_minor,
            risk_flags=signals.risk_flags,
            ai_action=signals.ai_action,
            draft_accepted=signals.draft_accepted,
            validator_passed=signals.validator_passed,
            validation_codes=signals.validation_codes,
            critic_verdict=signals.critic_verdict,
            citations_dropped=signals.citations_dropped,
        ),
        now,
    )
