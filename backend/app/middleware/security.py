import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security headers to all responses.

    In production mode, includes HSTS and a strict CSP.
    In development mode, omits HSTS (HTTP) and relaxes CSP for localhost.
    """

    # Swagger UI loads scripts/styles from cdn.jsdelivr.net and uses inline styles
    DOCS_PATHS = {"/docs", "/openapi.json", "/redoc"}
    DOCS_CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: https://fastapi.tiangolo.com"
    )

    def __init__(self, app, environment: str = "development"):
        super().__init__(app)
        self.is_production = environment == "production"
        self.csp = os.getenv(
            "CSP_POLICY",
            "default-src 'self'" if self.is_production else "default-src 'self' http://localhost:*",
        )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "0"
        if request.url.path in self.DOCS_PATHS:
            response.headers["Content-Security-Policy"] = self.DOCS_CSP
        else:
            response.headers["Content-Security-Policy"] = self.csp
        if self.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
