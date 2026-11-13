"""The durable checkpointer: where a paused run's place in the graph is kept (ADR-011).

LangGraph saves the run's state after every step, keyed by `workflow_runs.thread_id`. A run
that pauses (waiting for a reviewer or the customer) can therefore be resumed later, by a
fresh graph in a fresh process, from exactly where it stopped.

The official Postgres checkpointer uses psycopg 3, not asyncpg. It gets its own small
connection pool here; the rest of the app stays on SQLAlchemy + asyncpg. Both point at the
same database, using the same `DATABASE_URL` (only the driver prefix differs).

Two safety details:

- `setup()` creates and migrates the checkpointer's own tables. Two processes starting at
  once must not both migrate, so it runs under a Postgres advisory lock. It is idempotent.
- Checkpoints are read back with a strict allowlist of Python types (our state's types and
  the tool output types). Anything else in a checkpoint is refused, never imported.
"""

import asyncio
import dataclasses
import sys
import types
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, get_args, get_origin, get_type_hints

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from support_agent.tools.base import ToolRegistry
from support_agent.workflow.state import WorkflowState

# Any fixed number works; it only has to be the same in every process.
_SETUP_LOCK_ID = 7_310_011
_POOL_MAX_SIZE = 5


def psycopg_url(database_url: str) -> str:
    """Our URL names the asyncpg driver; psycopg wants the plain postgresql:// form."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _collect_types(hint: Any, found: set[type]) -> None:
    """Add every class reachable from a type hint: models, dataclasses, enums."""
    if get_origin(hint) is not None or isinstance(hint, types.UnionType):
        for arg in get_args(hint):  # list[X], X | None, dict[str, X], Literal[...]
            _collect_types(arg, found)
        return
    if not isinstance(hint, type) or hint in found or hint.__module__ == "builtins":
        return
    if issubclass(hint, BaseModel):
        found.add(hint)
        for field in hint.model_fields.values():
            _collect_types(field.annotation, found)
    elif dataclasses.is_dataclass(hint):
        found.add(hint)
        for field_hint in get_type_hints(hint).values():
            _collect_types(field_hint, found)
    elif issubclass(hint, Enum):
        found.add(hint)


def allowed_types(registry: ToolRegistry) -> set[type]:
    """The classes a checkpoint may contain: the workflow state's and the tools' outputs.

    Tool results are typed `BaseModel` in the state, so the concrete output classes are
    taken from the registry the run uses.
    """
    found: set[type] = set()
    for hint in get_type_hints(WorkflowState).values():
        _collect_types(hint, found)
    for model in registry.output_models:
        _collect_types(model, found)
    return found


def checkpoint_serializer(registry: ToolRegistry) -> JsonPlusSerializer:
    """LangGraph's serializer, restricted to our own types (nothing else is ever imported)."""
    allowed: Iterable[type] = allowed_types(registry)
    return JsonPlusSerializer(allowed_msgpack_modules=list(allowed))


async def setup_checkpointer(saver: AsyncPostgresSaver, pool: AsyncConnectionPool[Any]) -> None:
    """Create or migrate the checkpoint tables, one process at a time."""
    async with pool.connection() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(%s)", (_SETUP_LOCK_ID,))
        try:
            await saver.setup()  # uses another pooled connection; idempotent
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_LOCK_ID,))


def _require_selector_loop() -> None:
    """Fail at once with the fix, instead of psycopg retrying its connection forever."""
    if sys.platform == "win32" and isinstance(
        asyncio.get_running_loop(), asyncio.ProactorEventLoop
    ):
        raise RuntimeError(
            "psycopg needs the selector event loop on Windows. Start the API with "
            "--loop support_agent.core.event_loop:selector_loop_factory (ADR-011)."
        )


@asynccontextmanager
async def open_checkpointer(
    database_url: str, registry: ToolRegistry
) -> AsyncIterator[AsyncPostgresSaver]:
    """A ready-to-use checkpointer with its own pool, closed on exit.

    autocommit: the saver manages its own statements (and setup's CREATE INDEX CONCURRENTLY
    cannot run in a transaction). prepare_threshold=0: no server-side prepared statements,
    which transaction-mode poolers such as Supabase's cannot keep. dict_row: the saver
    reads rows by column name.
    """
    _require_selector_loop()
    pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
        conninfo=psycopg_url(database_url),
        min_size=1,
        max_size=_POOL_MAX_SIZE,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool, serde=checkpoint_serializer(registry))
        await setup_checkpointer(saver, pool)
        yield saver
    finally:
        await pool.close()
