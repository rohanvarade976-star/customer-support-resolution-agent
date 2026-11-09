import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UpdatedAt, UUIDPrimaryKey, str_enum
from support_agent.domain.enums import RefundOperationStatus


class RefundOperation(UUIDPrimaryKey, CreatedAt, UpdatedAt, Base):
    """One attempt to move money back to a customer.

    "Total refunds never exceed the paid amount" spans many rows, so a CHECK
    cannot enforce it. The refund service enforces it (M8) while holding
    SELECT ... FOR UPDATE on the order row.
    """

    __tablename__ = "refund_operations"
    __table_args__ = (CheckConstraint("amount_minor > 0", name="amount_minor_positive"),)

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("action_proposals.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)  # paise
    # Sent to the payment provider. Unique, so the same refund can never be recorded twice.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    provider_reference: Mapped[str | None] = mapped_column(String(255))  # provider's refund id
    status: Mapped[RefundOperationStatus] = mapped_column(
        str_enum(RefundOperationStatus, "status"),
        default=RefundOperationStatus.RESERVED,
        server_default=RefundOperationStatus.RESERVED.value,
    )
