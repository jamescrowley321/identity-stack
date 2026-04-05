"""RFC 9457 Problem Detail responses and Result-to-HTTP mapping.

result_to_response() is the single translation point between service Result types
and HTTP responses. Routers call this instead of constructing responses manually.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from expression import Result
from fastapi import Request
from fastapi.encoders import jsonable_encoder
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

logger = logging.getLogger(__name__)


class ProblemDetailResponse(BaseModel):
    """RFC 9457 Problem Details for HTTP APIs."""

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    traceId: str | None = None  # noqa: N815 — RFC 9457 camelCase extension field


@dataclass(frozen=True)
class _ErrorMapping:
    """Structured mapping for an error type to its RFC 9457 fields."""

    uri: str
    status: int
    title: str


# Error type -> structured mapping (URI path, HTTP status, title)
_ERROR_TYPE_MAP: dict[type[IdentityError], _ErrorMapping] = {
    NotFound: _ErrorMapping(uri="/errors/not-found", status=404, title="Resource Not Found"),
    Conflict: _ErrorMapping(uri="/errors/conflict", status=409, title="Conflict"),
    ValidationError: _ErrorMapping(uri="/errors/validation", status=422, title="Validation Error"),
    SyncFailed: _ErrorMapping(uri="/errors/sync-failed", status=207, title="Sync Partial Success"),
    ProviderError: _ErrorMapping(uri="/errors/provider-error", status=502, title="Provider Error"),
    Forbidden: _ErrorMapping(uri="/errors/forbidden", status=403, title="Forbidden"),
}

# Sanitized messages for error types where upstream details may leak.
_SANITIZED_DETAIL: dict[type[IdentityError], str] = {
    ProviderError: "An upstream provider error occurred. Check server logs for details.",
    SyncFailed: "The local operation succeeded but upstream synchronisation failed.",
    Forbidden: "You do not have permission to perform this action.",
}


def _get_trace_id() -> str:
    """Extract OTel trace ID from current span context, if available."""
    try:
        from opentelemetry import trace  # noqa: PLC0415

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001
        logger.warning("Failed to retrieve OTel trace ID; tracing may be misconfigured", exc_info=True)
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

    Uses ``jsonable_encoder`` to safely serialise Pydantic models, dataclasses,
    UUIDs, datetimes, and other non-primitive types before passing to
    ``JSONResponse``.
    """
    match result:
        case Result(tag="ok", ok=value):
            try:
                return JSONResponse(content=jsonable_encoder(value), status_code=status)
            except (TypeError, ValueError) as exc:
                logger.error("Failed to serialize Ok value (type=%s): %s", type(value).__name__, exc)
                return _internal_error(request, "Response serialization failed")
        case Result(tag="error", error=err):
            if not isinstance(err, IdentityError):
                logger.error(
                    "Result error branch contained non-IdentityError (type=%s): %r",
                    type(err).__name__,
                    err,
                )
                return _internal_error(request, "Unexpected error type")
            return _error_to_problem_detail(err, request)
        case _:
            logger.error("Unexpected result type in result_to_response: %r", result)
            return _internal_error(request, "Unexpected result type")


def _internal_error(request: Request, detail: str) -> JSONResponse:
    """Return a 500 Problem Detail response for unexpected failures."""
    return JSONResponse(
        content=ProblemDetailResponse(
            type="/errors/unknown",
            title="Internal Error",
            status=500,
            detail=detail,
            instance=str(request.url.path),
            traceId=_get_trace_id() or None,
        ).model_dump(exclude_none=True),
        status_code=500,
        media_type="application/problem+json",
    )


def _error_to_problem_detail(err: IdentityError, request: Request) -> JSONResponse:
    """Convert an IdentityError to an RFC 9457 Problem Detail JSON response."""
    error_type = type(err)
    mapping = _ERROR_TYPE_MAP.get(error_type)

    if mapping is None:
        logger.error("Unregistered IdentityError subclass %s: %s", error_type.__name__, err.message)
        uri, http_status, title = "/errors/unknown", 500, "Internal Error"
    else:
        uri, http_status, title = mapping.uri, mapping.status, mapping.title

    # Log provider errors and forbidden responses for operator visibility
    if isinstance(err, ProviderError):
        logger.error("ProviderError on %s: %s", request.url.path, err.message)
    elif isinstance(err, Forbidden):
        logger.warning("Forbidden on %s: %s", request.url.path, err.message)
    elif isinstance(err, SyncFailed):
        logger.warning(
            "SyncFailed on %s (operation=%s): %s",
            request.url.path,
            getattr(err, "operation", ""),
            err.message,
        )

    # Sanitise detail for error types that may leak upstream internals
    detail = _SANITIZED_DETAIL.get(error_type, err.message)

    trace_id = _get_trace_id() or None
    problem = ProblemDetailResponse(
        type=uri,
        title=title,
        status=http_status,
        detail=detail,
        instance=str(request.url.path),
        traceId=trace_id,
    )

    return JSONResponse(
        content=problem.model_dump(exclude_none=True),
        status_code=http_status,
        media_type="application/problem+json",
    )
