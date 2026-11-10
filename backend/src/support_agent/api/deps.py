"""FastAPI dependencies: the database session and "who is calling".

Every protected route depends on one of these, so identity always comes from the
verified token and never from the request body.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings, get_settings
from support_agent.core.errors import AppError
from support_agent.db.session import get_sessionmaker
from support_agent.domain.enums import Role
from support_agent.schemas.auth import Principal
from support_agent.services import auth as auth_service

# auto_error=False so a missing header goes through our own error format.
_bearer = HTTPBearer(auto_error=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


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
