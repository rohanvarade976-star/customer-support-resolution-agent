"""Loads verified order facts for one customer and applies the pure eligibility rules.

Every lookup is scoped to the customer id, so another customer's order is
indistinguishable from a missing one (ORDER_NOT_FOUND, FR-2).

The refund window comes from the refund policy version in force on the order's placed_at
date (ADR-005). Proposals, refund execution and the eligibility tool all use `load_order`,
so they always apply the same window as the policy the agents cite.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import Order, PolicyDocument, Return
from support_agent.domain.eligibility import EligibilityDecision, OrderFacts, check_eligibility
from support_agent.domain.enums import ReturnStatus
from support_agent.domain.money import refund_totals
from support_agent.domain.policies import REFUND_POLICY_KEY, policy_date
from support_agent.repositories import orders, policies, refunds
from support_agent.schemas.order_status import RefundableBalanceResponse
from support_agent.services.orders import order_not_found


@dataclass(frozen=True)
class LoadedOrder:
    """The facts plus the few identifying details other services need."""

    order_id: UUID
    order_number: str
    currency: str
    facts: OrderFacts
    # The refund policy version in force on the order's placed_at date; None if none applies.
    refund_policy: PolicyDocument | None = None


def _latest_active_return(order: Order) -> Return | None:
    # order.returns is sorted oldest first; a cancelled return counts as no return.
    active = [ret for ret in order.returns if ret.status != ReturnStatus.CANCELLED]
    return active[-1] if active else None


async def load_order(session: AsyncSession, customer_id: UUID, order_number: str) -> LoadedOrder:
    order = await orders.get_for_customer(session, customer_id, order_number)
    if order is None:
        raise order_not_found()
    succeeded, in_progress = refund_totals(await refunds.sum_by_status(session, order.id))
    ret = _latest_active_return(order)
    policy = await policies.get_policy_in_force(
        session, REFUND_POLICY_KEY, policy_date(order.placed_at)
    )
    facts = OrderFacts(
        order_status=order.status,
        delivered_at=order.delivered_at,
        return_status=None if ret is None else ret.status,
        # A return's request time is its creation time.
        return_requested_at=None if ret is None else ret.created_at,
        return_received_at=None if ret is None else ret.received_at,
        inspection_status=None if ret is None else ret.inspection_status,
        paid_amount_minor=order.paid_amount_minor,
        succeeded_refunds_minor=succeeded,
        in_progress_refunds_minor=in_progress,
        refund_window_days=None if policy is None else policy.refund_window_days,
    )
    return LoadedOrder(order.id, order.order_number, order.currency, facts, policy)


async def check_refund_eligibility(
    session: AsyncSession,
    customer_id: UUID,
    order_number: str,
    now: datetime,
) -> EligibilityDecision:
    loaded = await load_order(session, customer_id, order_number)
    return check_eligibility(loaded.facts, now)


async def get_refundable_balance(
    session: AsyncSession, customer_id: UUID, order_number: str
) -> RefundableBalanceResponse:
    loaded = await load_order(session, customer_id, order_number)
    return RefundableBalanceResponse(
        order_number=loaded.order_number,
        currency=loaded.currency,
        paid_amount_minor=loaded.facts.paid_amount_minor,
        succeeded_refunds_minor=loaded.facts.succeeded_refunds_minor,
        in_progress_refunds_minor=loaded.facts.in_progress_refunds_minor,
        refundable_balance_minor=loaded.facts.balance_minor,
    )
