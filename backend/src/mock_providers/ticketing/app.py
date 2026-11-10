"""Mock ticketing provider: tickets and notes stored in Postgres.

Creating a ticket and adding a note both need an Idempotency-Key header, so a retry
after a lost response never creates a duplicate:
- same key + same body      -> 200 with the original (no new row),
- same key + different body -> 409 IDEMPOTENCY_KEY_REUSED,
- no key                    -> 400 IDEMPOTENCY_KEY_MISSING.

Run locally (from backend/):
    uv run uvicorn mock_providers.ticketing.app:create_app --factory --port 8003
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Response
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mock_providers.common.admin import register_admin_routes
from mock_providers.common.clock import Clock, utc_now
from mock_providers.common.errors import ProviderError, register_error_handlers
from mock_providers.common.faults import FaultInjector, install_fault_middleware
from mock_providers.common.idempotency import (
    IdempotencyKeyHeader,
    check_idempotency_key,
    key_reused_error,
)
from mock_providers.ticketing import storage
from mock_providers.ticketing.config import TicketingSettings
from mock_providers.ticketing.schemas import (
    NoteCreate,
    NoteResponse,
    TicketCreate,
    TicketResponse,
    TicketUpdate,
)


def ticket_not_found() -> ProviderError:
    return ProviderError(404, "TICKET_NOT_FOUND", "No ticket with this id.", retryable=False)


def lookup_failed() -> ProviderError:
    # Cannot happen: rows are never deleted by the API.
    return ProviderError(500, "PROVIDER_ERROR", "Idempotency lookup failed.", retryable=True)


def to_note_response(note: storage.StoredNote) -> NoteResponse:
    return NoteResponse(
        id=note.id, ticket_id=note.ticket_id, body=note.body, created_at=note.created_at
    )


def create_app(settings: TicketingSettings | None = None, clock: Clock = utc_now) -> FastAPI:
    """Build the app. Tests pass their own settings."""
    settings = settings or TicketingSettings()
    engine: AsyncEngine = create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )
    faults = FaultInjector()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await storage.create_schema(engine)
        yield
        await engine.dispose()

    app = FastAPI(title="Mock ticketing provider", lifespan=lifespan)
    install_fault_middleware(app, faults, "/tickets")
    register_error_handlers(app)
    register_admin_routes(app, faults, settings.app_env)

    async def to_ticket_response(ticket: storage.StoredTicket) -> TicketResponse:
        ticket_notes = await storage.list_notes(engine, ticket.id)
        return TicketResponse(
            id=ticket.id,
            customer_reference=ticket.customer_reference,
            order_reference=ticket.order_reference,
            subject=ticket.subject,
            description=ticket.description,
            status=ticket.status,
            created_at=ticket.created_at,
            updated_at=ticket.updated_at,
            notes=[to_note_response(note) for note in ticket_notes],
        )

    # ------------------------------------------------------------ tickets

    @app.post("/tickets", response_model=TicketResponse, status_code=201)
    async def create_ticket(
        body: TicketCreate,
        response: Response,
        idempotency_key: IdempotencyKeyHeader = None,
    ) -> TicketResponse:
        key = check_idempotency_key(idempotency_key)
        created = await storage.insert_ticket(
            engine,
            ticket_id=f"tkt_{uuid4().hex}",
            idempotency_key=key,
            request=body,
            created_at=clock(),
        )
        if created is not None:
            return await to_ticket_response(created)  # 201: brand-new ticket

        existing = await storage.get_ticket_by_idempotency_key(engine, key)
        if existing is None:
            raise lookup_failed()
        if not existing.same_payload_as(body):
            raise key_reused_error()
        response.status_code = 200  # replay: return the original ticket as it is now
        return await to_ticket_response(existing)

    @app.get("/tickets/{ticket_id}", response_model=TicketResponse)
    async def get_ticket(ticket_id: str) -> TicketResponse:
        ticket = await storage.get_ticket(engine, ticket_id)
        if ticket is None:
            raise ticket_not_found()
        return await to_ticket_response(ticket)

    @app.patch("/tickets/{ticket_id}", response_model=TicketResponse)
    async def update_ticket(ticket_id: str, body: TicketUpdate) -> TicketResponse:
        # Setting a status is naturally safe to repeat, so no idempotency key is needed.
        ticket = await storage.update_ticket_status(engine, ticket_id, body.status, clock())
        if ticket is None:
            raise ticket_not_found()
        return await to_ticket_response(ticket)

    # ------------------------------------------------------------ notes

    @app.post("/tickets/{ticket_id}/notes", response_model=NoteResponse, status_code=201)
    async def add_note(
        ticket_id: str,
        body: NoteCreate,
        response: Response,
        idempotency_key: IdempotencyKeyHeader = None,
    ) -> NoteResponse:
        key = check_idempotency_key(idempotency_key)
        if await storage.get_ticket(engine, ticket_id) is None:
            raise ticket_not_found()

        created = await storage.insert_note(
            engine,
            note_id=f"note_{uuid4().hex}",
            ticket_id=ticket_id,
            idempotency_key=key,
            request=body,
            created_at=clock(),
        )
        if created is not None:
            return to_note_response(created)  # 201: brand-new note

        existing = await storage.get_note_by_idempotency_key(engine, key)
        if existing is None:
            raise lookup_failed()
        if not existing.same_payload_as(ticket_id, body):
            raise key_reused_error()
        response.status_code = 200  # replay: return the original note
        return to_note_response(existing)

    return app
