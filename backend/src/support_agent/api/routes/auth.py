"""Login and "who am I". Routes stay thin: validate, call the service, return."""

from fastapi import APIRouter

from support_agent.api.deps import PrincipalDep, SessionDep, SettingsDep
from support_agent.schemas.auth import LoginRequest, MeResponse, TokenResponse
from support_agent.services import auth as auth_service

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
async def login(body: LoginRequest, session: SessionDep, settings: SettingsDep) -> TokenResponse:
    return await auth_service.login(session, settings, email=body.email, password=body.password)


@router.get("/me")
async def me(principal: PrincipalDep) -> MeResponse:
    return MeResponse(
        id=principal.id, role=principal.role, name=principal.name, email=principal.email
    )
