"""One error shape for the whole API: {success: false, error_code, message}.

Routes and services raise `AppError`; the handlers below turn it, validation
errors, unknown routes and unexpected crashes into that same JSON body.
"""

import logging
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    success: Literal[False] = False
    error_code: str
    message: str


class AppError(Exception):
    """An expected failure with an HTTP status and a stable machine-readable code."""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.headers = headers


def error_response(
    status_code: int, error_code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = ErrorBody(error_code=error_code, message=message)
    return JSONResponse(status_code=status_code, content=body.model_dump(), headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.status_code, exc.error_code, exc.message, exc.headers)

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
