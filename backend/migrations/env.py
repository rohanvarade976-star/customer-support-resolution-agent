"""Alembic environment: how migrations connect to the database (async).

URL priority:
1. `config.attributes["database_url"]`, set by code that calls Alembic directly
   (the integration tests use this to target the test database).
2. DATABASE_URL from app settings, for `uv run alembic ...` on the command line.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from support_agent.core.config import get_settings
from support_agent.db.base import CHECKPOINT_TABLES, Base
from support_agent.db.models import *  # noqa: F403  (registers every table on Base.metadata)

config = context.config

# Use alembic.ini's logging setup only on the command line, not when tests call us.
if config.config_file_name is not None and "database_url" not in config.attributes:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Leave LangGraph's checkpoint tables (and their indexes) to the checkpointer."""
    table = getattr(obj, "table", obj)  # an index or column points at its table
    table_name = name if type_ == "table" else getattr(table, "name", None)
    return table_name not in CHECKPOINT_TABLES


def get_url() -> str:
    url: str | None = config.attributes.get("database_url")
    return url or get_settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`: print the SQL instead of running it."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, include_object=include_object
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(get_url())
    async with engine.connect() as connection:
        # Alembic itself is synchronous; run_sync hands it a sync-style connection.
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
