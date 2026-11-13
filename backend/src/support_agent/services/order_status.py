"""Return, payment and shipment status for one customer's order (read only)."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.domain.policies import SHIPPING_POLICY_KEY, policy_date
from support_agent.integrations.shipping import Shipment, ShippingClient
from support_agent.repositories import orders, policies, refunds
from support_agent.schemas.order_status import (
    PaymentStatusResponse,
    RefundSummary,
    ReturnStatusResponse,
)
from support_agent.services.eligibility import load_order
from support_agent.services.orders import order_not_found


async def get_return_status(
    session: AsyncSession, customer_id: UUID, order_number: str
) -> ReturnStatusResponse:
    loaded = await load_order(session, customer_id, order_number)
    facts = loaded.facts
    return ReturnStatusResponse(
        order_number=loaded.order_number,
        return_status=facts.return_status,
        requested_at=facts.return_requested_at,
        received_at=facts.return_received_at,
        inspection_status=facts.inspection_status,
    )


async def get_payment_status(
    session: AsyncSession, customer_id: UUID, order_number: str
) -> PaymentStatusResponse:
    loaded = await load_order(session, customer_id, order_number)
    operations = await refunds.list_for_order(session, loaded.order_id)
    return PaymentStatusResponse(
        order_number=loaded.order_number,
        currency=loaded.currency,
        paid_amount_minor=loaded.facts.paid_amount_minor,
        succeeded_refunds_minor=loaded.facts.succeeded_refunds_minor,
        in_progress_refunds_minor=loaded.facts.in_progress_refunds_minor,
        refunds=[
            RefundSummary(status=op.status, amount_minor=op.amount_minor, created_at=op.created_at)
            for op in operations
        ],
    )


async def get_shipment(
    session: AsyncSession, shipping: ShippingClient, customer_id: UUID, order_number: str
) -> Shipment:
    # Ownership is checked first, so the provider is never asked about someone else's order.
    if await orders.find_id_for_customer(session, customer_id, order_number) is None:
        raise order_not_found()
    return await shipping.get_shipment(order_number)


async def get_investigation_threshold(
    session: AsyncSession, customer_id: UUID, order_number: str
) -> int | None:
    """The shipping policy's investigation threshold in force on the order's placed date.

    None when no shipping policy applies: the caller then must not promise an investigation.
    """
    order = await orders.get_for_customer(session, customer_id, order_number)
    if order is None:
        raise order_not_found()
    policy = await policies.get_policy_in_force(
        session, SHIPPING_POLICY_KEY, policy_date(order.placed_at)
    )
    return None if policy is None else policy.investigation_threshold_days
