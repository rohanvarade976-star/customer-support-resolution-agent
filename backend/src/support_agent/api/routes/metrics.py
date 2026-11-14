"""GET /metrics: Prometheus text format, behind a bearer token (ADR-018).

Not public: metrics reveal volumes, failure rates and model names. With no `METRICS_TOKEN`
configured the route answers 404, so a missing secret never means an open endpoint.
"""

import hmac
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from support_agent.api.deps import ClockDep, SessionDep, SettingsDep
from support_agent.core import metrics
from support_agent.core.errors import AppError
from support_agent.services.metrics_collector import refresh_database_gauges

router = APIRouter(tags=["metrics"])


def _check_token(expected: str, authorization: str | None) -> None:
    scheme, _, supplied = (authorization or "").partition(" ")
    # compare_digest: the check takes the same time however many characters match.
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise AppError(
            401,
            "UNAUTHENTICATED",
            "A valid metrics token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    if settings.metrics_token is None:
        raise AppError(404, "NOT_FOUND", "Not Found")
    _check_token(settings.metrics_token.get_secret_value(), authorization)
    now: datetime = clock()
    await refresh_database_gauges(session, now)
    body = generate_latest(metrics.REGISTRY) + generate_latest(metrics.DB_REGISTRY)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)
