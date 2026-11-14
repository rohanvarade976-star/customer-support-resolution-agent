"""FastAPI dependencies: the database session, "who is calling", the clock, the
payment and shipping provider clients, the embedder, the LLM client, the checkpointer, and
the `EnqueueEnv` that requests use to queue work for the background worker.

Every protected route depends on one of these, so identity always comes from the
verified token and never from the request body. Tests override the clock, the
payment and shipping clients, the embedder and the LLM client (fixed time, in-memory
mock providers, fake embedder, fake LLM) and the checkpointer (the test database).

Since M7 Task 2 the API never runs the workflow, so no route needs the LLM client or a
`WorkflowEnv`. The worker builds those itself (worker/main.py); the tests build them from
these same dependencies (tests/integration/conftest.py), so their overrides still apply.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings, get_settings
from support_agent.core.errors import AppError
from support_agent.db.session import get_sessionmaker
from support_agent.domain.enums import Role
from support_agent.integrations.payments import PaymentClient
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import LLMClient
from support_agent.llm.providers import build_llm_client
from support_agent.rag import embeddings
from support_agent.schemas.auth import Principal
from support_agent.services import auth as auth_service
from support_agent.services import rate_limits as rate_limit_service
from support_agent.services.runs import EnqueueEnv
from support_agent.workflow.deps import BranchSessions
from support_agent.workflow.graph import Checkpointer

# auto_error=False so a missing header goes through our own error format.
_bearer = HTTPBearer(auto_error=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def _new_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def get_branch_sessions() -> BranchSessions:
    """Opens a separate short-lived session for each parallel workflow branch.

    One AsyncSession must not be used by two tasks at once. Tests override this with a
    function that hands every branch the rolled-back test session, one branch at a time.
    """
    return _new_session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
# The same session, but closed when the route function returns, BEFORE the response body is
# sent. For streaming routes: a request-scoped session would stay open (in a transaction)
# for as long as the stream does.
ShortSessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


def get_session_factory() -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """Opens one short-lived session per use. A live stream polls with it (ADR-019).

    Tests override it to hand out the rolled-back test session.
    """
    return _new_session


SessionFactoryDep = Annotated[
    Callable[[], AbstractAsyncContextManager[AsyncSession]], Depends(get_session_factory)
]

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def get_clock() -> Clock:
    return utc_now


ClockDep = Annotated[Clock, Depends(get_clock)]


async def get_payment_client(settings: SettingsDep) -> AsyncIterator[PaymentClient]:
    async with PaymentClient(settings) as client:
        yield client


PaymentClientDep = Annotated[PaymentClient, Depends(get_payment_client)]


async def get_shipping_client(settings: SettingsDep) -> AsyncIterator[ShippingClient]:
    async with ShippingClient(settings) as client:
        yield client


def get_embedder(settings: SettingsDep) -> embeddings.Embedder:
    """The process-wide embedder (loaded on first use). Tests override it with a fake."""
    return embeddings.get_embedder(settings)


@lru_cache
def get_llm_client() -> LLMClient:
    """One LLM client for the whole process, built on first use (raises if misconfigured)."""
    return build_llm_client(get_settings())


def get_checkpointer(request: Request) -> Checkpointer:
    """The process-wide checkpointer, opened (and set up) by the app's lifespan in main.py."""
    return cast(Checkpointer, request.app.state.checkpointer)


CheckpointerDep = Annotated[Checkpointer, Depends(get_checkpointer)]


def get_enqueue_env(
    settings: SettingsDep, clock: ClockDep, checkpointer: CheckpointerDep
) -> EnqueueEnv:
    """For routes that queue work for the worker: no model, no provider clients."""
    return EnqueueEnv(settings, clock, checkpointer)


EnqueueEnvDep = Annotated[EnqueueEnv, Depends(get_enqueue_env)]


async def get_current_principal(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise auth_service.unauthenticated()
    return await auth_service.authenticate_token(session, settings, credentials.credentials)


PrincipalDep = Annotated[Principal, Depends(get_current_principal)]


async def get_streaming_principal(
    session: ShortSessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """The same check as `get_current_principal`, on a session closed before streaming."""
    if credentials is None:
        raise auth_service.unauthenticated()
    return await auth_service.authenticate_token(session, settings, credentials.credentials)


StreamingPrincipalDep = Annotated[Principal, Depends(get_streaming_principal)]


def require_customer(principal: PrincipalDep) -> Principal:
    if principal.role != Role.CUSTOMER:
        raise AppError(403, "FORBIDDEN", "This action is for customers only.")
    return principal


def require_reviewer(principal: PrincipalDep) -> Principal:
    # Admins are the higher role, so they pass reviewer checks too.
    if principal.role not in (Role.REVIEWER, Role.ADMIN):
        raise AppError(403, "FORBIDDEN", "This action is for reviewers only.")
    return principal


CustomerDep = Annotated[Principal, Depends(require_customer)]


# Rate limits for the two customer actions that start model work (ADR-017). Used as route
# dependencies: they run after the caller is authenticated and before any work is queued.
async def limit_run_starts(
    principal: CustomerDep, session: SessionDep, settings: SettingsDep, clock: ClockDep
) -> None:
    await rate_limit_service.check_run_start(
        session, settings, customer_id=str(principal.id), now=clock()
    )


async def limit_customer_replies(
    principal: CustomerDep, session: SessionDep, settings: SettingsDep, clock: ClockDep
) -> None:
    await rate_limit_service.check_customer_reply(
        session, settings, customer_id=str(principal.id), now=clock()
    )


ReviewerDep = Annotated[Principal, Depends(require_reviewer)]
