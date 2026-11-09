import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from support_agent.db.base import Base
from support_agent.db.models import *  # noqa: F403  (registers every table)


async def _table_names(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        names = {row.tablename for row in rows}
    await engine.dispose()
    return names


async def _has_vector_extension(url: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.connect() as conn:
        found = await conn.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    await engine.dispose()
    return found == 1


async def test_migrations_apply_cleanly_to_empty_database(
    empty_database: str, alembic_cfg: Config
) -> None:
    assert await _table_names(empty_database) == set()

    # Alembic is synchronous and starts its own event loop, so run it in a thread.
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")

    # Every model has a table, and nothing else exists besides Alembic's bookkeeping.
    expected = set(Base.metadata.tables) | {"alembic_version"}
    assert await _table_names(empty_database) == expected
    assert await _has_vector_extension(empty_database)

    # The models and the migrations agree: autogenerate would produce no changes.
    await asyncio.to_thread(command.check, alembic_cfg)

    # Downgrade works too, then upgrade again so later tests get a full schema.
    await asyncio.to_thread(command.downgrade, alembic_cfg, "base")
    assert await _table_names(empty_database) == {"alembic_version"}
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
