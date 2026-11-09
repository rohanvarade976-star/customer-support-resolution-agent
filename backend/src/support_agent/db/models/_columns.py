"""Column building blocks shared by every model."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum, func
from sqlalchemy.orm import Mapped, mapped_column


def str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """A status column stored as plain text, guarded by a CHECK constraint.

    native_enum=False: store VARCHAR, not a Postgres ENUM type. Adding a value
    later is then a simple constraint change instead of an ALTER TYPE.
    `name` becomes part of the CHECK name, e.g. ck_tickets_status.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,  # SQLAlchemy 2 does not add the CHECK unless asked
        length=32,
        # Store "completed" (the value), not "COMPLETED" (the Python member name).
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,  # reject unknown strings in Python before they reach SQL
    )


class UUIDPrimaryKey:
    # Python creates the id (so it is known before INSERT); the server default
    # covers rows inserted with raw SQL. sort_order=-1 puts `id` first in the table.
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
        sort_order=-1,
    )


class CreatedAt:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), sort_order=100)


class UpdatedAt:
    # onupdate is applied by SQLAlchemy when a row is updated through the ORM.
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), sort_order=101
    )
