"""Middleware factory — sole location for deployment-mode-conditional logic.

DEPLOYMENT_MODE is evaluated once at import time (ADR-GW-4, ADR-GW-5).
Valid values: "standalone" (default), "gateway".

v2 upgrade path: Replace os.getenv('DEPLOYMENT_MODE') with
OpenFeature client.get_string_value('deployment_mode', 'standalone')
for hot-toggle and per-feature flag support.
"""

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.middleware import SlowAPIMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.correlation import CorrelationIdMiddleware
from app.middleware.security import SecurityHeadersMiddleware

logger = logging.getLogger(__name__)

_VALID_MODES = ("standalone", "gateway")

DEPLOYMENT_MODE: str = os.getenv("DEPLOYMENT_MODE", "standalone").strip()

if DEPLOYMENT_MODE not in _VALID_MODES:
    raise ValueError(f"Invalid DEPLOYMENT_MODE={DEPLOYMENT_MODE!r}. Valid values: {', '.join(_VALID_MODES)}")

# Middleware that will be conditionally excluded in gateway mode (Story 2.2).
_GATEWAY_EXCLUDED = {"TokenValidationMiddleware", "SlowAPIMiddleware"}


def configure_middleware(app: FastAPI) -> None:
    """Register the middleware stack on *app*.

    Middleware is added innermost-first; the last ``add_middleware`` call
    becomes the outermost layer that processes the request first.

    In Story 3.1 all middleware is registered regardless of mode.
    Conditional assembly (skip TokenValidation & SlowAPI in gateway mode)
    is deferred to Story 2.2.
    """
    if getattr(app, "_middleware_configured", False):
        return
    app._middleware_configured = True

    raw_hosts = os.getenv("TRUSTED_PROXY_HOSTS", "127.0.0.1")
    trusted_hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()] or ["127.0.0.1"]

    # 1. CORS — innermost
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:3000")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 2. Token validation — skips public paths
    app.add_middleware(
        TokenValidationMiddleware,
        descope_project_id=os.getenv("DESCOPE_PROJECT_ID", ""),
        excluded_paths={"/api/health", "/api/validate-id-token", "/docs", "/redoc", "/openapi.json"},
    )

    # 3. Rate limiting — SlowAPIMiddleware registers state on request.state;
    #    enforcement is via @limiter decorators on route handlers.
    app.add_middleware(SlowAPIMiddleware)

    # 4. Security headers — set on all responses including 429s
    app.add_middleware(SecurityHeadersMiddleware, environment=os.getenv("ENVIRONMENT", "development"))

    # 5. Correlation ID — generates/validates X-Correlation-ID for request tracing
    app.add_middleware(CorrelationIdMiddleware)

    # 6. Proxy headers — outermost, sets request.client from X-Forwarded-For
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_hosts)

    # --- Startup log ---
    middleware_names = [m.cls.__name__ for m in reversed(app.user_middleware)]
    if DEPLOYMENT_MODE == "gateway":
        included = [n for n in middleware_names if n not in _GATEWAY_EXCLUDED]
        excluded = [n for n in middleware_names if n in _GATEWAY_EXCLUDED]
        logger.info(
            "Middleware configured — mode=%s, included=%s, excluded_in_story_2.2=%s",
            DEPLOYMENT_MODE,
            included,
            excluded,
        )
    else:
        logger.info(
            "Middleware configured — mode=%s, stack=%s",
            DEPLOYMENT_MODE,
            middleware_names,
        )
