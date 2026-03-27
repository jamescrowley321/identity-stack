import logging
import os

from limits import parse_many
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "60/minute")
RATE_LIMIT_AUTH = os.getenv("RATE_LIMIT_AUTH", "10/minute")


def _validate_rate_limit(value: str, name: str) -> None:
    """Validate that a rate limit string is parseable by the limits library."""
    items = parse_many(value)
    if not items:
        raise ValueError(f"Invalid {name} rate limit format '{value}'. Expected format like '60/minute' or '10/hour'.")


_validate_rate_limit(RATE_LIMIT_DEFAULT, "RATE_LIMIT_DEFAULT")
_validate_rate_limit(RATE_LIMIT_AUTH, "RATE_LIMIT_AUTH")


def get_rate_limit_key(request: Request) -> str:
    """Return authenticated user's sub claim if available, otherwise client IP."""
    try:
        claims = getattr(request.state, "claims", None)
        if claims and isinstance(claims, dict):
            sub = claims.get("sub")
            if sub:
                return str(sub)
    except (AttributeError, TypeError):
        logger.debug("Could not extract claims for rate limit key, falling back to IP")
    return get_remote_address(request)


limiter = Limiter(key_func=get_rate_limit_key, default_limits=[RATE_LIMIT_DEFAULT])


def _get_retry_after(exc: RateLimitExceeded) -> str:
    """Derive Retry-After seconds from the rate limit that was exceeded."""
    try:
        return str(int(exc.limit.limit.get_expiry()))
    except (AttributeError, TypeError):
        pass
    # Parse window from detail string (e.g. "10 per 1 minute")
    detail = getattr(exc, "detail", "") or ""
    if "hour" in detail:
        return "3600"
    if "second" in detail:
        return "1"
    # Default to 60s (1-minute window) as conservative fallback
    return "60"


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return a JSON 429 response with Retry-After header."""
    response = JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
    retry_after = _get_retry_after(exc)
    view_rate_limit = getattr(request.state, "view_rate_limit", None)
    if view_rate_limit:
        try:
            response = request.app.state.limiter._inject_headers(response, view_rate_limit)
        except Exception:
            logger.debug("_inject_headers failed, setting Retry-After manually")
            response.headers["Retry-After"] = retry_after
    else:
        response.headers["Retry-After"] = retry_after
    return response
