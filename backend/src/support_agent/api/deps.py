"""FastAPI dependencies: the database session, "who is calling", the clock, the
payment and shipping provider clients, the embedder, the LLM client, the checkpointer, and
the `WorkflowEnv` bundles that hand all of them to a run.

Every protected route depends on one of these, so identity always comes from the
verified token and never from the request body. Tests override the clock, the
payment and shipping clients, the embedder and the LLM client (fixed time, in-memory
mock providers, fake embedder, fake LLM) and the checkpointer (the test database).
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
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
from support_agent.llm.disabled import DisabledLLMClient
from support_agent.llm.providers import build_llm_client
from support_agent.rag import embeddings
from support_agent.schemas.auth import Principal
from support_agent.services import auth as auth_service
from support_agent.services.runs import WorkflowEnv
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
BranchSessionsDep = Annotated[BranchSessions, Depends(get_branch_sessions)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

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


ShippingClientDep = Annotated[ShippingClient, Depends(get_shipping_client)]


def get_embedder(settings: SettingsDep) -> embeddings.Embedder:
    """The process-wide embedder (loaded on first use). Tests override it with a fake."""
    return embeddings.get_embedder(settings)


EmbedderDep = Annotated[embeddings.Embedder, Depends(get_embedder)]


@lru_cache
def get_llm_client() -> LLMClient:
    """One LLM client for the whole process, built on first use (raises if misconfigured)."""
    return build_llm_client(get_settings())


LLMClientDep = Annotated[LLMClient, Depends(get_llm_client)]


def get_checkpointer(request: Request) -> Checkpointer:
    """The process-wide checkpointer, opened (and set up) by the app's lifespan in main.py."""
    return cast(Checkpointer, request.app.state.checkpointer)


CheckpointerDep = Annotated[Checkpointer, Depends(get_checkpointer)]


def get_workflow_env(
    settings: SettingsDep,
    llm: LLMClientDep,
    shipping_client: ShippingClientDep,
    payments: PaymentClientDep,
    embedder: EmbedderDep,
    clock: ClockDep,
    branch_sessions: BranchSessionsDep,
    checkpointer: CheckpointerDep,
) -> WorkflowEnv:
    """For starting a run and for customer replies: the agents may call the model."""
    return WorkflowEnv(
        settings, llm, shipping_client, payments, embedder, clock, branch_sessions, checkpointer
    )


def get_decision_env(
    settings: SettingsDep,
    shipping_client: ShippingClientDep,
    payments: PaymentClientDep,
    embedder: EmbedderDep,
    clock: ClockDep,
    branch_sessions: BranchSessionsDep,
    checkpointer: CheckpointerDep,
) -> WorkflowEnv:
    """For the reviewer's decision: what follows is fixed code, so the model is disabled."""
    return WorkflowEnv(
        settings,
        DisabledLLMClient(),
        shipping_client,
        payments,
        embedder,
        clock,
        branch_sessions,
        checkpointer,
    )


WorkflowEnvDep = Annotated[WorkflowEnv, Depends(get_workflow_env)]
DecisionEnvDep = Annotated[WorkflowEnv, Depends(get_decision_env)]


async def get_current_principal(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise auth_service.unauthenticated()
    return await auth_service.authenticate_token(session, settings, credentials.credentials)


PrincipalDep = Annotated[Principal, Depends(get_current_principal)]


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
ReviewerDep = Annotated[Principal, Depends(require_reviewer)]
