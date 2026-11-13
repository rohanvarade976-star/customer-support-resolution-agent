"""Load the demo scenarios from data/seed/scenarios.json into the database.

Dates in the JSON are "N days ago", turned into timestamps relative to the `now` the
caller passes in. The scripts pass the demo anchor (core/demo_anchor.py), the same
instant the mock shipping provider counts back from, so both always agree.

`seed()` and `reset()` only flush; the caller owns the transaction and commits,
so a whole seed or reset either fully happens or not at all.

This is a developer tool, not part of the request path, so it uses the ORM
directly instead of going through repositories.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import ColumnElement, CursorResult, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import get_settings
from support_agent.db.base import Base
from support_agent.db.models import (
    ActionProposal,
    Approval,
    AuditEvent,
    Customer,
    CustomerMemory,
    Job,
    Order,
    OrderItem,
    RefundOperation,
    Return,
    StaffUser,
    Ticket,
    WorkflowRun,
)
from support_agent.domain.enums import (
    ActionType,
    ApprovalDecision,
    InspectionStatus,
    OrderStatus,
    ProposalStatus,
    RefundOperationStatus,
    ReturnStatus,
    RunStatus,
    StaffRole,
    TicketCategory,
    TicketStatus,
)
from support_agent.domain.proposals import payload_hash

# seed.py -> db -> support_agent -> src -> backend
SEED_FILE = Path(__file__).resolve().parents[3] / "data" / "seed" / "scenarios.json"

# Every table the seed writes to or reset deletes from, in foreign-key-safe
# delete order (children before parents). Used for the printed summaries.
SEED_TABLES: list[type[Base]] = [
    RefundOperation,
    Approval,
    ActionProposal,
    AuditEvent,
    Job,
    WorkflowRun,
    CustomerMemory,
    Ticket,
    Return,
    OrderItem,
    Order,
    Customer,
    StaffUser,
]

APPROVAL_TTL = timedelta(hours=24)


class ResetRefusedError(RuntimeError):
    """Raised when reset is attempted where it must never run."""


# ---------------------------------------------------------------- JSON shape


class _SeedModel(BaseModel):
    # A typo in the JSON (e.g. "deliverd_days_ago") fails loudly instead of being ignored.
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeedItem(_SeedModel):
    product_id: str
    product_name: str
    quantity: int
    paid_amount_minor: int  # paise, for the whole line


class SeedReturn(_SeedModel):
    status: ReturnStatus
    requested_days_ago: int
    received_days_ago: int | None = None
    inspection_status: InspectionStatus = InspectionStatus.PENDING


class SeedRefundHistory(_SeedModel):
    """A past refund that already went ticket -> run -> proposal -> approval -> refund."""

    ticket_summary: str
    reason: str
    opened_days_ago: int
    completed_days_ago: int
    reviewer_id: str
    provider_reference: str


class SeedOrder(_SeedModel):
    order_number: str
    status: OrderStatus
    placed_days_ago: int
    delivered_days_ago: int | None = None
    # No orders column for this; kept for the mock shipping provider (later milestone).
    shipped_days_ago: int | None = None
    items: list[SeedItem]
    returns: list[SeedReturn] = []
    refund_history: SeedRefundHistory | None = None

    @model_validator(mode="after")
    def _delivered_orders_have_a_date(self) -> "SeedOrder":
        if (self.status == OrderStatus.DELIVERED) != (self.delivered_days_ago is not None):
            raise ValueError(f"{self.order_number}: delivered_days_ago must be set iff delivered")
        return self

    @property
    def paid_amount_minor(self) -> int:
        return sum(item.paid_amount_minor for item in self.items)


class SeedCustomer(_SeedModel):
    name: str
    email: str
    external_auth_subject: str
    orders: list[SeedOrder]


class SeedStaff(_SeedModel):
    name: str
    email: str
    role: StaffRole


class SeedData(_SeedModel):
    staff: list[SeedStaff] = []
    customers: list[SeedCustomer]


def load_scenarios(path: Path = SEED_FILE) -> SeedData:
    return SeedData.model_validate_json(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- seeding


def _empty_counts() -> dict[str, int]:
    return {model.__tablename__: 0 for model in SEED_TABLES}


class _Seeder:
    """Holds `now`, the session and the running counts while one seed runs."""

    def __init__(self, session: AsyncSession, now: datetime, password_hash: str) -> None:
        self.session = session
        self.now = now
        # One hash shared by every demo account (they all use the same demo password).
        self.password_hash = password_hash
        self.created = _empty_counts()

    def days_ago(self, days: int) -> datetime:
        return self.now - timedelta(days=days)

    async def add[M: Base](self, row: M) -> M:
        # Flush right away so the row's id exists for the children that reference it.
        self.session.add(row)
        await self.session.flush()
        self.created[row.__tablename__] += 1
        return row

    async def customer(self, data: SeedCustomer) -> Customer:
        existing = await self.session.scalar(select(Customer).where(Customer.email == data.email))
        if existing is not None:
            if existing.password_hash is None:  # seeded before logins existed
                existing.password_hash = self.password_hash
                await self.session.flush()
            return existing
        return await self.add(
            Customer(
                name=data.name,
                email=data.email,
                external_auth_subject=data.external_auth_subject,
                password_hash=self.password_hash,
            )
        )

    async def staff(self, data: SeedStaff) -> None:
        exists = await self.session.scalar(
            select(StaffUser.id).where(StaffUser.email == data.email)
        )
        if exists is None:
            await self.add(StaffUser(password_hash=self.password_hash, **data.model_dump()))

    async def order(self, customer: Customer, data: SeedOrder) -> None:
        exists = await self.session.scalar(
            select(Order.id).where(Order.order_number == data.order_number)
        )
        if exists is not None:
            return  # already seeded; its items, returns and history came with it

        placed_at = self.days_ago(data.placed_days_ago)
        order = await self.add(
            Order(
                order_number=data.order_number,
                customer_id=customer.id,
                status=data.status,
                paid_amount_minor=data.paid_amount_minor,
                placed_at=placed_at,
                delivered_at=(
                    None
                    if data.delivered_days_ago is None
                    else self.days_ago(data.delivered_days_ago)
                ),
                created_at=placed_at,
            )
        )
        for item in data.items:
            await self.add(OrderItem(order_id=order.id, created_at=placed_at, **item.model_dump()))
        for ret in data.returns:
            await self.add(
                Return(
                    order_id=order.id,
                    status=ret.status,
                    inspection_status=ret.inspection_status,
                    received_at=(
                        None
                        if ret.received_days_ago is None
                        else self.days_ago(ret.received_days_ago)
                    ),
                    created_at=self.days_ago(ret.requested_days_ago),
                )
            )
        if data.refund_history is not None:
            await self.refund_history(order, data.refund_history)

    async def refund_history(self, order: Order, data: SeedRefundHistory) -> None:
        """A completed refund that satisfies every constraint a real one would."""
        opened = self.days_ago(data.opened_days_ago)
        completed = self.days_ago(data.completed_days_ago)

        ticket = await self.add(
            Ticket(
                customer_id=order.customer_id,
                order_id=order.id,
                category=TicketCategory.REFUND,
                status=TicketStatus.RESOLVED,
                summary=data.ticket_summary,
                created_at=opened,
            )
        )
        run = await self.add(
            WorkflowRun(
                ticket_id=ticket.id,
                thread_id=f"seed-{order.order_number}",
                status=RunStatus.COMPLETED,
                started_at=opened,
                completed_at=completed,
            )
        )
        payload: dict[str, Any] = {
            "order_id": str(order.id),
            "order_number": order.order_number,
            "amount_minor": order.paid_amount_minor,
            "currency": order.currency,
            "reason": data.reason,
        }
        digest = payload_hash(payload)
        proposal = await self.add(
            ActionProposal(
                run_id=run.id,
                action_type=ActionType.REQUEST_REFUND_APPROVAL,
                payload=payload,
                payload_hash=digest,
                status=ProposalStatus.EXECUTED,
                created_at=opened,
            )
        )
        await self.add(
            Approval(
                proposal_id=proposal.id,
                payload_hash=digest,  # must match the proposal (composite foreign key)
                reviewer_id=data.reviewer_id,
                decision=ApprovalDecision.APPROVED,
                created_at=opened,
                expires_at=opened + APPROVAL_TTL,
                decided_at=opened + timedelta(hours=1),
            )
        )
        await self.add(
            RefundOperation(
                order_id=order.id,
                proposal_id=proposal.id,
                amount_minor=order.paid_amount_minor,  # full refund
                idempotency_key=f"seed-refund-{order.order_number}",
                provider_reference=data.provider_reference,
                status=RefundOperationStatus.SUCCEEDED,
                created_at=completed,
            )
        )


async def seed(
    session: AsyncSession, data: SeedData, now: datetime, password_hash: str
) -> dict[str, int]:
    """Insert any scenario rows that are missing. Returns rows created per table.

    Customers and staff are matched on email and orders on order_number, so running
    this twice creates nothing the second time. `password_hash` is the Argon2 hash
    of the demo password; it is also filled in for customers that have none yet.
    """
    seeder = _Seeder(session, now, password_hash)
    for staff_data in data.staff:
        await seeder.staff(staff_data)
    for customer_data in data.customers:
        customer = await seeder.customer(customer_data)
        for order_data in customer_data.orders:
            await seeder.order(customer, order_data)
    return seeder.created


# ---------------------------------------------------------------- reset


async def reset(session: AsyncSession, data: SeedData) -> dict[str, int]:
    """Delete the seeded customers and everything that belongs to them.

    This includes rows the app created later for these customers (demo runs),
    so the next seed starts clean. Returns rows deleted per table.
    """
    app_env = get_settings().app_env
    if app_env == "production":
        raise ResetRefusedError("Refusing to reset seed data: APP_ENV is 'production'.")

    emails = [customer.email for customer in data.customers]
    customer_ids = select(Customer.id).where(Customer.email.in_(emails))
    order_ids = select(Order.id).where(Order.customer_id.in_(customer_ids))
    ticket_ids = select(Ticket.id).where(Ticket.customer_id.in_(customer_ids))
    run_ids = select(WorkflowRun.id).where(WorkflowRun.ticket_id.in_(ticket_ids))
    proposal_ids = select(ActionProposal.id).where(ActionProposal.run_id.in_(run_ids))

    # Same order as SEED_TABLES: children first, so no foreign key is ever violated.
    conditions: dict[type[Base], ColumnElement[bool]] = {
        RefundOperation: or_(
            RefundOperation.order_id.in_(order_ids),
            RefundOperation.proposal_id.in_(proposal_ids),
        ),
        Approval: Approval.proposal_id.in_(proposal_ids),
        ActionProposal: ActionProposal.id.in_(proposal_ids),
        AuditEvent: AuditEvent.run_id.in_(run_ids),
        Job: Job.run_id.in_(run_ids),
        WorkflowRun: WorkflowRun.id.in_(run_ids),
        CustomerMemory: CustomerMemory.customer_id.in_(customer_ids),
        Ticket: Ticket.customer_id.in_(customer_ids),
        Return: Return.order_id.in_(order_ids),
        OrderItem: OrderItem.order_id.in_(order_ids),
        Order: Order.id.in_(order_ids),
        Customer: Customer.id.in_(customer_ids),
        StaffUser: StaffUser.email.in_([staff.email for staff in data.staff]),
    }
    deleted = _empty_counts()
    for model in SEED_TABLES:
        result = await session.execute(delete(model).where(conditions[model]))
        # A DELETE always returns a CursorResult; the type stubs only say Result.
        deleted[model.__tablename__] = cast(CursorResult[Any], result).rowcount
    return deleted
