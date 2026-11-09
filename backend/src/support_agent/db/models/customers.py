from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UpdatedAt, UUIDPrimaryKey


class Customer(UUIDPrimaryKey, CreatedAt, UpdatedAt, Base):
    __tablename__ = "customers"

    # The `sub` claim of the customer's JWT. This is how a login maps to a customer row.
    external_auth_subject: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
