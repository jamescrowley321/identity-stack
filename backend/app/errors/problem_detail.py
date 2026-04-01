"""RFC 9457 Problem Detail responses and Result-to-HTTP mapping.

result_to_response() is the single translation point between service Result types
and HTTP responses. Routers call this instead of constructing responses manually.
"""

from __future__ import annotations

from typing import Any

from expression import Error, Ok, Result
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.errors.identity import (
    Conflict,
    Forbidden,
    IdentityError,
    NotFound,
    ProviderError,
    SyncFailed,
    ValidationError,
)


class ProblemDetailResponse(BaseModel):
    """RFC 9457 Problem Details for HTTP APIs."""

    type: str
    title: str
    status: int
    detail: str
    instance: str = ""
    traceId: str = ""  # noqa: N815 — RFC 9457 camelCase extension field


# Error type -> (URI path, HTTP status, title)
_ERROR_TYPE_MAP: dict[type[IdentityError], tuple[str, int, str]] = {
    NotFound: ("/errors/not-found", 404, "Resource Not Found"),
    Conflict: ("/errors/conflict", 409, "Conflict"),
    ValidationError: ("/errors/validation", 422, "Validation Error"),
    SyncFailed: ("/errors/sync-failed", 202, "Sync Pending"),
    ProviderError: ("/errors/provider-error", 502, "Provider Error"),
    Forbidden: ("/errors/forbidden", 403, "Forbidden"),
}


def _get_trace_id() -> str:
    """Extract OTel trace ID from current span context, if available."""
    try:
        from opentelemetry import trace  # noqa: PLC0415

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x")
    except (ImportError, AttributeError):
        pass
    return ""


def result_to_response(
    result: Result[Any, IdentityError],
    request: Request,
    *,
    status: int = 200,
) -> JSONResponse:
    """Map a service Result to an HTTP response.

    Ok(value) -> JSONResponse with the given success status code.
    Error(err) -> RFC 9457 Problem Detail response.
    """
    match result:
        case Ok(value):
            return JSONResponse(content=value, status_code=status)
        case Error(err):
            return _error_to_problem_detail(err, request)


def _error_to_problem_detail(err: IdentityError, request: Request) -> JSONResponse:
    """Convert an IdentityError to an RFC 9457 Problem Detail JSON response."""
    error_type = type(err)
    uri, http_status, title = _ERROR_TYPE_MAP.get(
        error_type,
        ("/errors/unknown", 500, "Internal Error"),
    )

    problem = ProblemDetailResponse(
        type=uri,
        title=title,
        status=http_status,
        detail=err.message,
        instance=str(request.url.path),
        traceId=_get_trace_id(),
    )

    return JSONResponse(
        content=problem.model_dump(),
        status_code=http_status,
        media_type="application/problem+json",
    )
