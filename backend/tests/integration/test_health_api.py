from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.api.deps import get_session


async def test_health_is_ok_when_the_database_answers(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_health_is_503_when_the_database_is_down(app: FastAPI) -> None:
    class BrokenSession:
        async def execute(self, *_args: Any, **_kwargs: Any) -> None:
            raise ConnectionError("password=hunter2 host=db.internal")

    async def broken_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, BrokenSession())

    app.dependency_overrides[get_session] = broken_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "success": False,
        "error_code": "DATABASE_UNAVAILABLE",
        "message": "The database is not reachable.",
    }  # nothing from the underlying error leaks out
