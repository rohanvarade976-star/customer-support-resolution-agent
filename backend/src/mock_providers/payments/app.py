"""Mock payment provider: a small FastAPI app that behaves like a real refund API.

It imitates the parts of a real provider that are easy to get wrong:
- idempotency keys (same key + same payload = same refund, never a second one),
- refunds that finish later (submitted -> processing -> succeeded | failed),
- switchable failures, including a lost response after the refund was saved.

Run locally (from backend/):
    uv run uvicorn mock_providers.payments.app:create_app --factory --port 8001
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException

from mock_providers.payments import storage
from mock_providers.payments.config import PaymentsSettings
from mock_providers.payments.faults import FaultInjector
from mock_providers.payments.lifecycle import current_status
from mock_providers.payments.schemas import (
    ErrorBody,
    FaultConfig,
    FaultMode,
    FaultState,
    FinalOutcome,
    RefundRequest,
    RefundResponse,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ProviderError(Exception):
    """Raised by a route to send a structured error response."""

    def __init__(self, status_code: int, error_code: str, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = ErrorBody(error_code=error_code, retryable=retryable, message=message)


def error_response(
    status_code: int, error_code: str, message: str, retryable: bool
) -> JSONResponse:
    body = ErrorBody(error_code=error_code, retryable=retryable, message=message)
    return JSONResponse(status_code=status_code, content=body.model_dump())


def _is_refunds_path(path: str) -> bool:
    return path == "/refunds" or path.startswith("/refunds/")


def create_app(settings: PaymentsSettings | None = None, clock: Clock = utc_now) -> FastAPI:
    """Build the app. Tests pass their own settings and a fake clock."""
    settings = settings or PaymentsSettings()
    engine: AsyncEngine = create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )
    faults = FaultInjector()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await storage.create_schema(engine)
        yield
        await engine.dispose()

    app = FastAPI(title="Mock payment provider", lifespan=lifespan)

    def to_response(refund: storage.StoredRefund) -> RefundResponse:
        status = current_status(
            created_at=refund.created_at,
            final_outcome=refund.final_outcome,
            now=clock(),
            submitted_seconds=settings.mock_payments_submitted_seconds,
            processing_seconds=settings.mock_payments_processing_seconds,
        )
        return RefundResponse(
            provider_reference=refund.provider_reference,
            idempotency_key=refund.idempotency_key,
            order_reference=refund.order_reference,
            amount_minor=refund.amount_minor,
            currency=refund.currency,
            reason=refund.reason,
            status=status,
            created_at=refund.created_at,
        )

    # ------------------------------------------------------------ fault injection

    @app.middleware("http")
    async def inject_faults(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not _is_refunds_path(request.url.path):
            return await call_next(request)  # admin routes are never faulted

        mode, delay_ms = await faults.take(is_create=request.method == "POST")
        if mode is FaultMode.SLOW:
            await asyncio.sleep(delay_ms / 1000)
        elif mode is FaultMode.ERROR:
            return error_response(500, "PROVIDER_ERROR", "Internal provider error.", True)

        request.state.decline = mode is FaultMode.DECLINE
        response = await call_next(request)  # the route runs fully, including the DB write

        if mode is FaultMode.DROP_RESPONSE:
            # The work is done, but the caller never sees the real reply.
            return error_response(504, "GATEWAY_TIMEOUT", "Provider did not respond in time.", True)
        return response

    # ------------------------------------------------------------ error handlers

    @app.exception_handler(ProviderError)
    async def handle_provider_error(_request: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body.model_dump())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        return error_response(422, "VALIDATION_ERROR", f"{where}: {first['msg']}", False)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        return error_response(exc.status_code, code, str(exc.detail), False)

    # ------------------------------------------------------------ refunds

    @app.post("/refunds", response_model=RefundResponse, status_code=201)
    async def create_refund(
        body: RefundRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> RefundResponse:
        if not idempotency_key or len(idempotency_key) > 255:
            raise ProviderError(
                400,
                "IDEMPOTENCY_KEY_MISSING",
                "Idempotency-Key header (1-255 chars) is required.",
                retryable=False,
            )

        decline = getattr(request.state, "decline", False)
        created = await storage.insert_refund(
            engine,
            provider_reference=f"re_{uuid4().hex}",
            idempotency_key=idempotency_key,
            request=body,
            final_outcome=FinalOutcome.FAILED if decline else FinalOutcome.SUCCEEDED,
            created_at=clock(),
        )
        if created is not None:
            return to_response(created)  # 201: brand-new refund

        # The key was used before. Same payload = replay; different payload = misuse.
        existing = await storage.get_by_idempotency_key(engine, idempotency_key)
        if existing is None:  # cannot happen: rows are never deleted by the API
            raise ProviderError(500, "PROVIDER_ERROR", "Idempotency lookup failed.", True)
        if not existing.same_payload_as(body):
            raise ProviderError(
                409,
                "IDEMPOTENCY_KEY_REUSED",
                "This Idempotency-Key was already used with a different request body.",
                retryable=False,
            )
        response.status_code = 200  # replay: return the original refund
        return to_response(existing)

    @app.get("/refunds/{provider_reference}", response_model=RefundResponse)
    async def get_refund(provider_reference: str) -> RefundResponse:
        refund = await storage.get_by_reference(engine, provider_reference)
        if refund is None:
            raise ProviderError(404, "REFUND_NOT_FOUND", "No refund with this reference.", False)
        return to_response(refund)

    @app.get("/refunds", response_model=RefundResponse)
    async def find_refund_by_key(idempotency_key: str) -> RefundResponse:
        """Reconciliation: 'did my request with this key create a refund?'"""
        refund = await storage.get_by_idempotency_key(engine, idempotency_key)
        if refund is None:
            raise ProviderError(404, "REFUND_NOT_FOUND", "No refund with this key.", False)
        return to_response(refund)

    # ------------------------------------------------------------ admin (not in production)

    if settings.app_env != "production":

        @app.post("/_admin/faults", response_model=FaultState)
        async def set_faults(config: FaultConfig) -> FaultState:
            return await faults.configure(config)

        @app.get("/_admin/faults", response_model=FaultState)
        async def get_faults() -> FaultState:
            return await faults.state()

    return app
