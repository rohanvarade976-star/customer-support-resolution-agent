"""Login and "who am I". Routes stay thin: validate, call the service, return."""

from fastapi import APIRouter, Request

from support_agent.api.deps import ClockDep, PrincipalDep, SessionDep, SettingsDep
from support_agent.schemas.auth import LoginRequest, MeResponse, TokenResponse
from support_agent.services import auth as auth_service
from support_agent.services import rate_limits

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> TokenResponse:
    # Before anything else, so a flood of guesses never reaches the password hash (ADR-017).
    # Behind a proxy, run uvicorn with --proxy-headers so this is the real client address.
    client_ip = request.client.host if request.client else "unknown"
    await rate_limits.check_login(
        session, settings, email=body.email, client_ip=client_ip, now=clock()
    )
    return await auth_service.login(session, settings, email=body.email, password=body.password)


@router.get("/me")
async def me(principal: PrincipalDep) -> MeResponse:
    return MeResponse(
        id=principal.id, role=principal.role, name=principal.name, email=principal.email
    )
