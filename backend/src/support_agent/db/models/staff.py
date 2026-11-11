from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UpdatedAt, UUIDPrimaryKey
from support_agent.domain.enums import StaffRole


class StaffUser(UUIDPrimaryKey, CreatedAt, UpdatedAt, Base):
    """A support reviewer or admin. Customers live in `customers`, not here."""

    __tablename__ = "staff_users"
    __table_args__ = (
        # Same list as StaffRole; changing it needs a migration.
        CheckConstraint(
            "role IN (" + ", ".join(f"'{r.value}'" for r in StaffRole) + ")", name="role"
        ),
    )

    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    role: Mapped[StaffRole] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255))
