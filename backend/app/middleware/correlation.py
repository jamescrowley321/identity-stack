import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.logging_config import correlation_id_var


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Generates a unique correlation ID for each request.

    Accepts an incoming X-Correlation-ID header for distributed tracing,
    otherwise generates a new UUID4. The ID is stored in request.state,
    the contextvars context, and echoed back in the response header.
    """

    async def dispatch(self, request: Request, call_next):
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request.state.correlation_id = cid
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = cid
            return response
        finally:
            correlation_id_var.reset(token)
