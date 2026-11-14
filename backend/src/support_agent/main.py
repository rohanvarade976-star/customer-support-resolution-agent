"""The FastAPI app factory.

Run locally (from backend/). On Windows the --loop option is required (core/event_loop.py):
    uv run uvicorn support_agent.main:create_app --factory --port 8000 `
        --loop support_agent.core.event_loop:selector_loop_factory
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from support_agent.api.routes import (
    approvals,
    auth,
    health,
    memory,
    metrics,
    orders,
    run_events,
    runs,
    tickets,
)
from support_agent.core.config import get_settings
from support_agent.core.errors import register_error_handlers
from support_agent.core.logging import configure_logging
from support_agent.core.tracing import configure_tracing, shutdown_tracing, span
from support_agent.db.session import get_engine
from support_agent.tools.read_tools import build_read_registry
from support_agent.workflow.checkpointer import open_checkpointer

RequestHandler = Callable[[Request], Awaitable[Response]]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_tracing(settings, "api")
    # The checkpointer's tables are created or migrated here, under a lock (ADR-011).
    database_url = settings.database_url.get_secret_value()
    async with open_checkpointer(database_url, build_read_registry()) as checkpointer:
        app.state.checkpointer = checkpointer
        logging.getLogger(__name__).info("api started")
        yield
    await get_engine().dispose()  # close pooled database connections cleanly
    shutdown_tracing()  # send spans still waiting in the batch


def create_app() -> FastAPI:
    app = FastAPI(title="Customer Support Resolution Agent", lifespan=lifespan)
    register_error_handlers(app)

    @app.middleware("http")
    async def trace_request(request: Request, call_next: RequestHandler) -> Response:
        """One span per request: method, route template and status only. Never the URL, query,
        headers or body. The route template (/runs/{run_id}) holds no actual ids."""
        with span("api.request", {"http.method": request.method}) as request_span:
            response = await call_next(request)
            route = request.scope.get("route")
            request_span.set("http.route", getattr(route, "path", "unmatched"))
            request_span.set("http.status_code", response.status_code)
            return response

    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(auth.router)
    app.include_router(orders.router)
    app.include_router(memory.router)
    app.include_router(tickets.router)
    app.include_router(runs.router)
    app.include_router(run_events.router)
    app.include_router(approvals.router)

    # Added last, so it is the outermost layer: a browser's preflight (OPTIONS) is answered
    # here. Only the listed origins (ADR-019). No cookies are used (the token travels in the
    # Authorization header), so credentials stay off.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Retry-After"],  # so the UI can say how long to wait after a 429
        max_age=600,
    )
    return app
