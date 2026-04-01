"""Middleware factory — sole location for deployment-mode-conditional logic.

Evaluates DEPLOYMENT_MODE once at import time (never per-request).

v2 upgrade path: replace os.getenv("DEPLOYMENT_MODE") with
OpenFeature client.get_string_value("deployment_mode", "standalone")
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

VALID_MODES = ("standalone", "gateway")

DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "standalone")
if DEPLOYMENT_MODE not in VALID_MODES:
    raise ValueError(f"Invalid DEPLOYMENT_MODE={DEPLOYMENT_MODE!r}. Must be one of {VALID_MODES}.")


def configure_middleware(app: FastAPI) -> None:
    """Register the middleware stack based on DEPLOYMENT_MODE.

    Middleware is added innermost-first; the last call becomes the outermost layer.

    In gateway mode, Tyk handles authentication (JWT) and rate limiting,
    so TokenValidationMiddleware and SlowAPIMiddleware are skipped.
    """
    trusted_proxy_hosts = os.getenv("TRUSTED_PROXY_HOSTS", "127.0.0.1").split(",")

    # 1. CORS — innermost (always)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:3000")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info("Middleware included: CORSMiddleware")

    # 2. Token validation — standalone only (Tyk handles JWT in gateway mode).
    #    In gateway mode, all protected endpoints remain fail-closed: request.state.claims
    #    is never populated, so require_role/require_permission return 401/403.
    #    Network-level isolation (e.g., K8s NetworkPolicy) ensures only Tyk reaches the backend.
    if DEPLOYMENT_MODE == "standalone":
        app.add_middleware(
            TokenValidationMiddleware,
            descope_project_id=os.getenv("DESCOPE_PROJECT_ID", ""),
            excluded_paths={
                "/api/health",
                "/api/validate-id-token",
                "/docs",
                "/redoc",
                "/openapi.json",
            },
        )
        logger.info("Middleware included: TokenValidationMiddleware")
    else:
        logger.info("Middleware excluded: TokenValidationMiddleware (gateway mode)")

    # 3. Rate limiting — standalone only (Tyk handles rate limiting in gateway mode)
    if DEPLOYMENT_MODE == "standalone":
        app.add_middleware(SlowAPIMiddleware)
        logger.info("Middleware included: SlowAPIMiddleware")
    else:
        logger.info("Middleware excluded: SlowAPIMiddleware (gateway mode)")

    # 4. Security headers — always
    app.add_middleware(SecurityHeadersMiddleware, environment=os.getenv("ENVIRONMENT", "development"))
    logger.info("Middleware included: SecurityHeadersMiddleware")

    # 5. Correlation ID — always
    app.add_middleware(CorrelationIdMiddleware)
    logger.info("Middleware included: CorrelationIdMiddleware")

    # 6. Proxy headers — outermost (always)
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_proxy_hosts)
    logger.info("Middleware included: ProxyHeadersMiddleware")

    logger.info("Deployment mode: %s — middleware stack configured", DEPLOYMENT_MODE)
