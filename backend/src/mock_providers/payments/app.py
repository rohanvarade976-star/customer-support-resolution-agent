"""Mock payment provider: a small FastAPI app that behaves like a real refund API.

It imitates the parts of a real provider that are easy to get wrong:
- idempotency keys (same key + same payload = same refund, never a second one),
- refunds that finish later (submitted -> processing -> succeeded | failed),
- switchable failures, including a lost response after the refund was saved.

Run locally (from backend/):
    uv run uvicorn mock_providers.payments.app:create_app --factory --port 8001
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mock_providers.common.admin import register_admin_routes
from mock_providers.common.clock import Clock, utc_now
from mock_providers.common.errors import ProviderError, register_error_handlers
from mock_providers.common.faults import (
    BASIC_MODES,
    FaultInjector,
    FaultMode,
    install_fault_middleware,
)
from mock_providers.common.idempotency import (
    IdempotencyKeyHeader,
    check_idempotency_key,
    key_reused_error,
)
from mock_providers.payments import storage
from mock_providers.payments.config import PaymentsSettings
from mock_providers.payments.lifecycle import current_status
from mock_providers.payments.schemas import FinalOutcome, RefundRequest, RefundResponse


def create_app(settings: PaymentsSettings | None = None, clock: Clock = utc_now) -> FastAPI:
    """Build the app. Tests pass their own settings and a fake clock."""
    settings = settings or PaymentsSettings()
    engine: AsyncEngine = create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )
    faults = FaultInjector(supported_modes=BASIC_MODES | {FaultMode.DECLINE})

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await storage.create_schema(engine)
        yield
        await engine.dispose()

    app = FastAPI(title="Mock payment provider", lifespan=lifespan)
    install_fault_middleware(app, faults, "/refunds")
    register_error_handlers(app)
    register_admin_routes(app, faults, settings.app_env)

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

    @app.post("/refunds", response_model=RefundResponse, status_code=201)
    async def create_refund(
        body: RefundRequest,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKeyHeader = None,
    ) -> RefundResponse:
        key = check_idempotency_key(idempotency_key)
        decline = getattr(request.state, "fault_mode", None) is FaultMode.DECLINE
        created = await storage.insert_refund(
            engine,
            provider_reference=f"re_{uuid4().hex}",
            idempotency_key=key,
            request=body,
            final_outcome=FinalOutcome.FAILED if decline else FinalOutcome.SUCCEEDED,
            created_at=clock(),
        )
        if created is not None:
            return to_response(created)  # 201: brand-new refund

        # The key was used before. Same payload = replay; different payload = misuse.
        existing = await storage.get_by_idempotency_key(engine, key)
        if existing is None:  # cannot happen: rows are never deleted by the API
            raise ProviderError(500, "PROVIDER_ERROR", "Idempotency lookup failed.", True)
        if not existing.same_payload_as(body):
            raise key_reused_error()
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

    return app
