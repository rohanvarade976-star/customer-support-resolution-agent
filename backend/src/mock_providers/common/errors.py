"""The structured error body every mock returns, and the handlers that produce it."""

from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorBody(BaseModel):
    """Every error the mocks return has this shape (matches the PRD failure format)."""

    success: Literal[False] = False
    error_code: str
    retryable: bool
    message: str


class ProviderError(Exception):
    """Raised by a route to send a structured error response."""

    def __init__(self, status_code: int, error_code: str, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = ErrorBody(error_code=error_code, retryable=retryable, message=message)


def error_response(
    status_code: int, error_code: str, message: str, retryable: bool
) -> JSONResponse:
    body = ErrorBody(error_code=error_code, retryable=retryable, message=message)
    return JSONResponse(status_code=status_code, content=body.model_dump())


def register_error_handlers(app: FastAPI) -> None:
    """Turn every error (ours, validation, 404 for unknown routes) into an ErrorBody."""

    @app.exception_handler(ProviderError)
    async def handle_provider_error(_request: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body.model_dump())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        return error_response(422, "VALIDATION_ERROR", f"{where}: {first['msg']}", False)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        return error_response(exc.status_code, code, str(exc.detail), False)
