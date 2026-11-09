"""The database itself rejects invalid money and ownership data.

Each test checks two things: the INSERT fails with the expected constraint,
and afterwards the table holds exactly the rows we expect (database state,
not just an error message).
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.base import Base
from support_agent.db.models import (
    ActionProposal,
    Approval,
    Customer,
    Order,
    RefundOperation,
    Ticket,
    WorkflowRun,
)
from support_agent.domain.enums import ActionType, OrderStatus

PAID = 249_900  # ₹2,499.00 in paise


async def count_rows(session: AsyncSession, model: type[Base]) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def add_customer(session: AsyncSession, name: str = "Priya") -> Customer:
    customer = Customer(
        external_auth_subject=f"auth|{uuid.uuid4().hex}",
        name=name,
        email=f"{uuid.uuid4().hex}@example.com",
    )
    session.add(customer)
    await session.flush()
    return customer


async def add_order(session: AsyncSession, customer: Customer, number: str) -> Order:
    order = Order(
        order_number=number,
        customer_id=customer.id,
        status=OrderStatus.DELIVERED,
        paid_amount_minor=PAID,
    )
    session.add(order)
    await session.flush()
    return order


async def add_proposal(session: AsyncSession, order: Order) -> ActionProposal:
    """Ticket -> run -> proposal: the chain a refund operation hangs off."""
    ticket = Ticket(customer_id=order.customer_id, order_id=order.id, summary="No refund yet")
    session.add(ticket)
    await session.flush()
    run = WorkflowRun(ticket_id=ticket.id, thread_id=str(uuid.uuid4()))
    session.add(run)
    await session.flush()
    payload = {"order_id": str(order.id), "amount_minor": PAID, "currency": "INR"}
    proposal = ActionProposal(
        run_id=run.id,
        action_type=ActionType.REQUEST_REFUND_APPROVAL,
        payload=payload,
        payload_hash=hashlib.sha256(repr(payload).encode()).hexdigest(),
    )
    session.add(proposal)
    await session.flush()
    return proposal


@pytest.mark.parametrize("amount", [-PAID, 0])
async def test_refund_amount_must_be_positive(session: AsyncSession, amount: int) -> None:
    order = await add_order(session, await add_customer(session), "ORD-1042")
    proposal = await add_proposal(session, order)

    # begin_nested = savepoint: the failed INSERT is undone, the session stays usable.
    with pytest.raises(IntegrityError, match="ck_refund_operations_amount_minor_positive"):
        async with session.begin_nested():
            session.add(
                RefundOperation(
                    order_id=order.id,
                    proposal_id=proposal.id,
                    amount_minor=amount,
                    idempotency_key="refund-key-1",
                )
            )

    assert await count_rows(session, RefundOperation) == 0


async def test_duplicate_idempotency_key_is_rejected(session: AsyncSession) -> None:
    order = await add_order(session, await add_customer(session), "ORD-1042")
    proposal = await add_proposal(session, order)

    def refund() -> RefundOperation:
        return RefundOperation(
            order_id=order.id,
            proposal_id=proposal.id,
            amount_minor=PAID,
            idempotency_key="refund-key-1",
        )

    session.add(refund())
    await session.flush()

    with pytest.raises(IntegrityError, match="uq_refund_operations_idempotency_key"):
        async with session.begin_nested():
            session.add(refund())

    assert await count_rows(session, RefundOperation) == 1  # exactly one refund recorded


async def test_order_requires_existing_customer(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="fk_orders_customer_id_customers"):
        async with session.begin_nested():
            session.add(
                Order(
                    order_number="ORD-1042",
                    customer_id=uuid.uuid4(),  # no such customer
                    status=OrderStatus.DELIVERED,
                    paid_amount_minor=PAID,
                )
            )

    assert await count_rows(session, Order) == 0


async def test_ticket_cannot_reference_another_customers_order(session: AsyncSession) -> None:
    priya = await add_customer(session, "Priya")
    arjun = await add_customer(session, "Arjun")
    priyas_order = await add_order(session, priya, "ORD-1042")

    with pytest.raises(IntegrityError, match="fk_tickets_order_id_customer_id_orders"):
        async with session.begin_nested():
            session.add(Ticket(customer_id=arjun.id, order_id=priyas_order.id, summary="Refund"))

    assert await count_rows(session, Ticket) == 0


async def test_approval_must_match_proposal_hash(session: AsyncSession) -> None:
    order = await add_order(session, await add_customer(session), "ORD-1042")
    proposal = await add_proposal(session, order)

    with pytest.raises(IntegrityError, match="fk_approvals_proposal_id_payload_hash"):
        async with session.begin_nested():
            session.add(
                Approval(
                    proposal_id=proposal.id,
                    payload_hash="0" * 64,  # a valid-looking hash of different contents
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )

    assert await count_rows(session, Approval) == 0
