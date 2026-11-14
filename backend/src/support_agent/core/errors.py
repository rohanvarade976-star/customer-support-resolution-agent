"""One error shape for the whole API: {success: false, error_code, message}.

Some errors add a `details` object with machine-readable extras (for example the
reason codes of a refused refund). It is left out of the body when there are none.

Routes and services raise `AppError`; the handlers below turn it, validation
errors, unknown routes and unexpected crashes into that same JSON body.
"""

import logging
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from support_agent.core import metrics

log = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    success: Literal[False] = False
    error_code: str
    message: str
    details: dict[str, Any] | None = None


class PermanentJobError(Exception):
    """A background job that can never succeed, however often it is retried.

    The worker marks the job dead at once (no retries) and escalates its run. `code` is
    stored on the job and in the audit event, so it must be a fixed code, never a message.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AppError(Exception):
    """An expected failure with an HTTP status and a stable machine-readable code."""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        headers: dict[str, str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.headers = headers
        self.details = details


def error_response(
    status_code: int,
    error_code: str,
    message: str,
    headers: dict[str, str] | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body = ErrorBody(error_code=error_code, message=message, details=details)
    content = body.model_dump(exclude_none=True)  # no "details" key unless there are some
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code == 429:
            scope = (exc.details or {}).get("scope", "unknown")
            metrics.RATE_LIMITED.labels(scope=str(scope)).inc()
        return error_response(
            exc.status_code, exc.error_code, exc.message, exc.headers, exc.details
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        return error_response(422, "VALIDATION_ERROR", f"{where}: {first['msg']}")

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        # Log the details for us; tell the client nothing about the internals.
        log.error("unhandled error", exc_info=exc)
        return error_response(500, "INTERNAL_ERROR", "Something went wrong.")
