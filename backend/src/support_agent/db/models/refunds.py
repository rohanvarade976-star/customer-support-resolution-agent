import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UpdatedAt, UUIDPrimaryKey, str_enum
from support_agent.domain.enums import RefundOperationStatus


class RefundOperation(UUIDPrimaryKey, CreatedAt, UpdatedAt, Base):
    """One attempt to move money back to a customer.

    "Total refunds never exceed the paid amount" spans many rows, so a CHECK
    cannot enforce it. services/refund_execution.py enforces it while holding
    SELECT ... FOR UPDATE on the order row.
    """

    __tablename__ = "refund_operations"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_minor_positive"),
        CheckConstraint("reconcile_checks >= 0", name="reconcile_checks_non_negative"),
        # Serves the reconciler's query: "operations whose next check is due".
        Index(
            "ix_refund_operations_next_reconcile_at",
            "next_reconcile_at",
            postgresql_where=text("next_reconcile_at IS NOT NULL"),
        ),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    # Unique: one approved proposal can never turn into two refunds.
    proposal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("action_proposals.id"), unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)  # paise
    # Sent to the payment provider. Unique, so the same refund can never be recorded twice.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    provider_reference: Mapped[str | None] = mapped_column(String(255))  # provider's refund id
    status: Mapped[RefundOperationStatus] = mapped_column(
        str_enum(RefundOperationStatus, "status"),
        default=RefundOperationStatus.RESERVED,
        server_default=RefundOperationStatus.RESERVED.value,
    )

    # ---- Reconciliation (services/reconciliation.py, ADR-014)
    # Every claim by a reconciler adds one. It is also the fencing token: a reconciler may
    # settle the operation only while the count is still the one it claimed.
    reconcile_checks: Mapped[int] = mapped_column(default=0, server_default="0")
    # When a reconciler may next check this operation; while one is checking, the end of its
    # lease. NULL: nothing left to check (a final status, or escalated to a person).
    next_reconcile_at: Mapped[datetime | None]
    # When the reconciler re-sent the refund request (at most once per operation, ADR-004).
    resent_at: Mapped[datetime | None]
    # The last check's failure, as a code only.
    last_reconcile_error: Mapped[str | None] = mapped_column(String(64))
