import re
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.logging_config import correlation_id_var

_VALID_CID = re.compile(r"^[a-zA-Z0-9\-_.]{1,128}$")


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Generates a unique correlation ID for each request.

    Accepts an incoming X-Correlation-ID header for distributed tracing,
    otherwise generates a new UUID4. Incoming IDs are validated to prevent
    log injection — only alphanumeric, hyphens, underscores, and dots
    (max 128 chars) are accepted. The ID is stored in request.state,
    the contextvars context, and echoed back in the response header.
    """

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("X-Correlation-ID", "")
        cid = incoming if incoming and _VALID_CID.match(incoming) else str(uuid.uuid4())
        request.state.correlation_id = cid
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = cid
            return response
        finally:
            correlation_id_var.reset(token)
