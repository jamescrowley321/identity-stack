import os

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "60/minute")
RATE_LIMIT_AUTH = os.getenv("RATE_LIMIT_AUTH", "10/minute")


def get_rate_limit_key(request: Request) -> str:
    """Return authenticated user's sub claim if available, otherwise client IP."""
    try:
        claims = getattr(request.state, "claims", None)
        if claims and isinstance(claims, dict):
            sub = claims.get("sub")
            if sub:
                return str(sub)
    except Exception:
        pass
    return get_remote_address(request)


limiter = Limiter(key_func=get_rate_limit_key, default_limits=[RATE_LIMIT_DEFAULT])


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return a JSON 429 response with Retry-After header."""
    response = JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
    response.headers["Retry-After"] = "60"
    view_rate_limit = getattr(request.state, "view_rate_limit", None)
    if view_rate_limit:
        response = request.app.state.limiter._inject_headers(response, view_rate_limit)
    return response
