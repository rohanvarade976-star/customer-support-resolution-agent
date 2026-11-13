"""Base class for all database models.

The naming convention gives every index and constraint a predictable name
(e.g. `ck_refund_operations_amount_minor_positive`). That matters because:
- Alembic needs names to drop or change constraints in later migrations.
- Error messages and tests can point at the exact rule that was broken.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",  # index
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",  # unique constraint
    "ck": "ck_%(table_name)s_%(constraint_name)s",  # check constraint
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",  # foreign key
    "pk": "pk_%(table_name)s",  # primary key
}


# Tables owned by LangGraph's Postgres checkpointer (workflow/checkpointer.py). It creates and
# migrates them itself, so Alembic must never compare, create or drop them.
CHECKPOINT_TABLES = frozenset(
    {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Default column types for Python type hints, so models stay short:
    # every datetime is timezone-aware (TIMESTAMPTZ) and every dict is JSONB.
    type_annotation_map = {  # noqa: RUF012  (SQLAlchemy reads this class attribute)
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }
