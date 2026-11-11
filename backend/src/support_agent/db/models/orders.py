import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UpdatedAt, UUIDPrimaryKey, str_enum
from support_agent.domain.enums import InspectionStatus, OrderStatus, ReturnStatus


class Order(UUIDPrimaryKey, CreatedAt, UpdatedAt, Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("order_number ~ '^ORD-[0-9]+$'", name="order_number_format"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso_code"),
        CheckConstraint("paid_amount_minor >= 0", name="paid_amount_minor_non_negative"),
        # Lets other tables reference (order id, owner) together, so the database
        # itself refuses to link a ticket to another customer's order.
        UniqueConstraint("id", "customer_id"),
    )

    order_number: Mapped[str] = mapped_column(String(20), unique=True)  # e.g. ORD-1042
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[OrderStatus] = mapped_column(str_enum(OrderStatus, "status"))
    currency: Mapped[str] = mapped_column(String(3), server_default="INR")
    paid_amount_minor: Mapped[int] = mapped_column(BigInteger)  # paise
    placed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    delivered_at: Mapped[datetime | None]

    # lazy="raise": touching these without loading them first is an error, not a hidden
    # extra query. Repositories load them up front with selectinload (no N+1).
    items: Mapped[list["OrderItem"]] = relationship(
        order_by="(OrderItem.created_at, OrderItem.id)", lazy="raise"
    )
    returns: Mapped[list["Return"]] = relationship(
        order_by="(Return.created_at, Return.id)", lazy="raise"
    )


class OrderItem(UUIDPrimaryKey, CreatedAt, Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("paid_amount_minor >= 0", name="paid_amount_minor_non_negative"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(64))  # catalogue SKU; no products table in v1
    product_name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int]
    paid_amount_minor: Mapped[int] = mapped_column(BigInteger)  # paise, for the whole line


class Return(UUIDPrimaryKey, CreatedAt, UpdatedAt, Base):
    __tablename__ = "returns"
    __table_args__ = (
        CheckConstraint(
            "status <> 'received' OR received_at IS NOT NULL", name="received_has_timestamp"
        ),
        # An item cannot pass or fail inspection before the warehouse has it.
        CheckConstraint(
            "inspection_status = 'pending' OR received_at IS NOT NULL",
            name="inspected_only_after_received",
        ),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    status: Mapped[ReturnStatus] = mapped_column(str_enum(ReturnStatus, "status"))
    received_at: Mapped[datetime | None]
    inspection_status: Mapped[InspectionStatus] = mapped_column(
        str_enum(InspectionStatus, "inspection_status"),
        default=InspectionStatus.PENDING,
        server_default=InspectionStatus.PENDING.value,
    )
