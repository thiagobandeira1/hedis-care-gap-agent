"""RFC 9457 problem details for every error the API emits (``application/problem+json``).

One vocabulary: :class:`ApiError` subclasses for the mapped cases (404 / 409 / 422 / 502 /
503), FastAPI's request-validation errors re-rendered as 422 problems (field paths and
messages only — never the offending input), Starlette's routing errors (404 / 405) and,
last, any unhandled exception as a 500 whose ``detail`` is the error class name alone.
Problem ``type`` values are stable URNs (``urn:caregap:problem:<code>``) and ``code`` repeats
the slug as an extension member, P6-style, so clients can switch on it without parsing URIs.
"""

from collections.abc import Sequence
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from caregap.p6.client import P6ContractError, P6Error, P6Unavailable, PatientNotFound

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_TYPE_PREFIX = "urn:caregap:problem:"


def problem_type(code: str) -> str:
    return PROBLEM_TYPE_PREFIX + code


class Problem(BaseModel):
    """The RFC 9457 body. ``errors`` carries the decision / request validation messages."""

    model_config = ConfigDict(frozen=True)

    type: str = "about:blank"
    title: str
    status: int
    detail: str = ""
    instance: str | None = None
    code: str = "about:blank"
    errors: list[str] | None = None


class ApiError(Exception):
    """An error with a fixed HTTP status and a problem ``code``; subclasses fix the status."""

    status: int = 500
    title: str = "Internal Server Error"
    default_code: str = "internal"

    def __init__(
        self, detail: str = "", *, code: str | None = None, errors: Sequence[str] | None = None
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code or self.default_code
        self.errors: list[str] | None = list(errors) if errors is not None else None

    def to_problem(self, instance: str | None = None) -> Problem:
        return Problem(
            type=problem_type(self.code),
            title=self.title,
            status=self.status,
            detail=self.detail,
            instance=instance,
            code=self.code,
            errors=self.errors,
        )


class NotFoundError(ApiError):
    status = 404
    title = "Not Found"
    default_code = "not-found"


class ConflictError(ApiError):
    status = 409
    title = "Conflict"
    default_code = "conflict"


class UnprocessableError(ApiError):
    status = 422
    title = "Unprocessable Content"
    default_code = "unprocessable"


class BadGatewayError(ApiError):
    status = 502
    title = "Bad Gateway"
    default_code = "p6-contract"


class ServiceUnavailableError(ApiError):
    status = 503
    title = "Service Unavailable"
    default_code = "p6-unavailable"


def p6_error(exc: P6Error) -> ApiError:
    """P6 taxonomy -> HTTP: 404 unknown patient, 503 unavailable, 502 contract breach.
    P6 messages carry ids and codes only (never record content), so they are safe to echo."""
    if isinstance(exc, PatientNotFound):
        return NotFoundError("patient not found", code="patient-not-found")
    if isinstance(exc, P6Unavailable):
        return ServiceUnavailableError(f"fhir-feature-service unavailable: {exc}")
    if isinstance(exc, P6ContractError):
        return BadGatewayError(f"fhir-feature-service contract error: {exc}")
    return ServiceUnavailableError(f"fhir-feature-service error: {type(exc).__name__}")


def problem_response(problem: Problem, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def _validation_messages(exc: RequestValidationError) -> list[str]:
    """``loc: msg`` per error — the offending input is never echoed back."""
    messages: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ()))
        messages.append(f"{loc or 'body'}: {error.get('msg', 'invalid')}")
    return messages


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return problem_response(exc.to_problem(request.url.path))

    @app.exception_handler(RequestValidationError)
    def _request_invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            Problem(
                type=problem_type("request-invalid"),
                title="Unprocessable Content",
                status=422,
                detail="request validation failed",
                instance=request.url.path,
                code="request-invalid",
                errors=_validation_messages(exc),
            )
        )

    @app.exception_handler(StarletteHTTPException)
    def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        phrase = HTTPStatus(exc.status_code).phrase
        return problem_response(
            Problem(
                title=phrase,
                status=exc.status_code,
                detail=str(exc.detail) if exc.detail is not None else phrase,
                instance=request.url.path,
            ),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Error class only: never a message, which might quote content.
        return problem_response(
            Problem(
                type=problem_type("internal"),
                title="Internal Server Error",
                status=500,
                detail=type(exc).__name__,
                instance=request.url.path,
                code="internal",
            )
        )


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` entries advertising problem+json for the given statuses."""
    return {
        status: {
            "model": Problem,
            "description": HTTPStatus(status).phrase,
            "content": {PROBLEM_MEDIA_TYPE: {}},
        }
        for status in statuses
    }
