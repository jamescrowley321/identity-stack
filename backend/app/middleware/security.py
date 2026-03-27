import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Patterns that effectively disable CSP protection
_PERMISSIVE_CSP_PATTERNS = {"*", "default-src *", "unsafe-inline", "unsafe-eval"}

_DEFAULT_PROD_CSP = "default-src 'self'"
_DEFAULT_DEV_CSP = "default-src 'self' http://localhost:*"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security headers to all responses.

    In production mode, includes HSTS and a strict CSP.
    In development mode, omits HSTS (HTTP) and relaxes CSP for localhost.
    """

    def __init__(self, app, environment: str = "development"):
        super().__init__(app)
        # M1: Case-insensitive environment check
        self.is_production = environment.strip().lower() == "production"
        self.csp = self._resolve_csp()

    def _resolve_csp(self) -> str:
        """Resolve CSP policy from env var with validation (M2)."""
        default = _DEFAULT_PROD_CSP if self.is_production else _DEFAULT_DEV_CSP
        csp = os.getenv("CSP_POLICY", "").strip()
        if not csp:
            return default
        # Warn on overly permissive values
        if any(pattern in csp.lower() for pattern in _PERMISSIVE_CSP_PATTERNS):
            logger.warning("CSP_POLICY contains permissive pattern — using default CSP instead")
            return default
        return csp

    async def dispatch(self, request: Request, call_next):
        # S1: Ensure headers are applied even if call_next raises
        try:
            response = await call_next(request)
        except Exception:
            response = Response("Internal Server Error", status_code=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Content-Security-Policy"] = self.csp
        # S3: Permissions-Policy
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        # S4: Cross-origin isolation headers
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if self.is_production:
            # S5: Add preload directive to HSTS
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
        return response
