"""The FastAPI app factory.

Run locally (from backend/):
    uv run uvicorn support_agent.main:create_app --factory --port 8000
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from support_agent.api.routes import auth, health
from support_agent.core.config import get_settings
from support_agent.core.errors import register_error_handlers
from support_agent.core.logging import configure_logging
from support_agent.db.session import get_engine


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level)
    logging.getLogger(__name__).info("api started")
    yield
    await get_engine().dispose()  # close pooled database connections cleanly


def create_app() -> FastAPI:
    app = FastAPI(title="Customer Support Resolution Agent", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    return app
