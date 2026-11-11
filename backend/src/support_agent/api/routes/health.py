import asyncio
import logging

from fastapi import APIRouter

from support_agent.api.deps import SessionDep
from support_agent.core.errors import AppError
from support_agent.repositories import health as health_repo
from support_agent.schemas.health import HealthResponse

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

DB_CHECK_TIMEOUT_SECONDS = 2


@router.get("/health")
async def health(session: SessionDep) -> HealthResponse:
    try:
        async with asyncio.timeout(DB_CHECK_TIMEOUT_SECONDS):
            await health_repo.ping(session)
    except Exception as error:  # any failure means "not healthy"; the cause goes to the log
        log.warning("health check failed", exc_info=error)
        raise AppError(503, "DATABASE_UNAVAILABLE", "The database is not reachable.") from error
    return HealthResponse()
