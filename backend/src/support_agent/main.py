"""The FastAPI app factory.

Run locally (from backend/). On Windows the --loop option is required (core/event_loop.py):
    uv run uvicorn support_agent.main:create_app --factory --port 8000 `
        --loop support_agent.core.event_loop:selector_loop_factory
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from support_agent.api.routes import approvals, auth, health, orders, runs, tickets
from support_agent.core.config import get_settings
from support_agent.core.errors import register_error_handlers
from support_agent.core.logging import configure_logging
from support_agent.db.session import get_engine
from support_agent.tools.read_tools import build_read_registry
from support_agent.workflow.checkpointer import open_checkpointer


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    # The checkpointer's tables are created or migrated here, under a lock (ADR-011).
    database_url = settings.database_url.get_secret_value()
    async with open_checkpointer(database_url, build_read_registry()) as checkpointer:
        app.state.checkpointer = checkpointer
        logging.getLogger(__name__).info("api started")
        yield
    await get_engine().dispose()  # close pooled database connections cleanly


def create_app() -> FastAPI:
    app = FastAPI(title="Customer Support Resolution Agent", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(orders.router)
    app.include_router(tickets.router)
    app.include_router(runs.router)
    app.include_router(approvals.router)
    return app
