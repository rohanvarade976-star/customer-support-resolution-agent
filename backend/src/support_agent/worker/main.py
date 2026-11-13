"""The background worker process. Run it next to the API (from backend/):

    uv run python -m support_agent.worker

It opens the same things the API does (settings, database, checkpointer) plus what the
workflow needs (the LLM client, provider clients, the embedder), then runs the loop in
worker/runner.py until Ctrl+C.

- Windows: psycopg (the checkpointer) needs the selector event loop, so this starts through
  `core.event_loop.run`, like the scripts (ADR-011).
- Ctrl+C (or SIGTERM) asks for a graceful stop: no new jobs, the current one may finish
  within the grace period, else it is released for another worker. A second Ctrl+C stops at
  once; the lease then runs out and another worker takes the job. Ctrl+Break on Windows is
  handled the same way, but only without `uv run`: uv itself exits on Ctrl+Break.
- Several workers can run at once: the queue's SKIP LOCKED claims keep them apart.
"""

import asyncio
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import FrameType

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import get_settings
from support_agent.core.event_loop import run
from support_agent.core.logging import configure_logging
from support_agent.db.session import get_engine, get_sessionmaker
from support_agent.integrations.payments import PaymentClient
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.disabled import DisabledLLMClient
from support_agent.llm.providers import build_llm_client
from support_agent.rag.embeddings import get_embedder
from support_agent.services.runs import WorkflowEnv
from support_agent.tools.read_tools import build_read_registry
from support_agent.worker.handlers import WorkerDeps
from support_agent.worker.runner import run_forever
from support_agent.workflow.checkpointer import open_checkpointer

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def _install_stop_handlers(stop: asyncio.Event) -> None:
    """Turn the first Ctrl+C / SIGTERM / Ctrl+Break into a graceful stop.

    `signal.signal` works on Windows too (the event loop's add_signal_handler does not).
    The handler runs between Python steps in the main thread, so it hands the request to
    the loop with call_soon_threadsafe, which also wakes the loop if it is waiting.
    """
    loop = asyncio.get_running_loop()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        log.info("shutdown_requested", extra={"signal": signal.Signals(signum).name})
        loop.call_soon_threadsafe(stop.set)
        # A second press stops at once.
        default = signal.default_int_handler if signum == signal.SIGINT else signal.SIG_DFL
        signal.signal(signum, default)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)  # SIGBREAK exists on Windows only
        if signum is not None:
            signal.signal(signum, request_stop)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    llm = build_llm_client(settings)  # fails here, at startup, if misconfigured
    embedder = get_embedder(settings)
    stop = asyncio.Event()
    _install_stop_handlers(stop)

    database_url = settings.database_url.get_secret_value()
    async with open_checkpointer(database_url, build_read_registry()) as checkpointer:

        @asynccontextmanager
        async def workflow_env(*, llm_enabled: bool) -> AsyncIterator[WorkflowEnv]:
            async with PaymentClient(settings) as payments, ShippingClient(settings) as shipping:
                yield WorkflowEnv(
                    settings=settings,
                    llm=llm if llm_enabled else DisabledLLMClient(),
                    shipping_client=shipping,
                    payments=payments,
                    embedder=embedder,
                    clock=utc_now,
                    branch_sessions=_session,  # one short-lived session per parallel branch
                    checkpointer=checkpointer,
                )

        deps = WorkerDeps(
            settings=settings,
            clock=utc_now,
            checkpointer=checkpointer,
            sessions=_session,
            workflow_env=workflow_env,
        )
        log.info("worker started")
        await run_forever(deps, stop)
    await get_engine().dispose()  # close pooled database connections cleanly
    log.info("worker stopped")


def start() -> None:
    run(main())
