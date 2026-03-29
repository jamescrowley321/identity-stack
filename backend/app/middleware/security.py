import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security headers to all responses.

    In production mode, includes HSTS and a strict CSP.
    In development mode, omits HSTS (HTTP) and relaxes CSP for localhost.
    """

    def __init__(self, app, environment: str = "development"):
        super().__init__(app)
        self.is_production = environment == "production"
        dev_csp = (
            "default-src 'self' http://localhost:*;"
            " style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com;"
            " script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.redoc.ly;"
            " font-src 'self' https://fonts.gstatic.com;"
            " img-src 'self' https://fastapi.tiangolo.com https://cdn.redoc.ly data:;"
            " worker-src blob:"
        )
        csp_value = os.getenv(
            "CSP_POLICY",
            "default-src 'self'" if self.is_production else dev_csp,
        )
        self.csp = csp_value.replace("\n", " ").replace("\r", " ")

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Content-Security-Policy"] = self.csp
        if self.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
