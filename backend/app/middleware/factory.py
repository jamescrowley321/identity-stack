"""Middleware factory — sole location for deployment-mode-conditional logic.

DEPLOYMENT_MODE is evaluated **once** at module import time (never per
request). The mode determines which middleware get registered, and the
factory is the only place where that branching happens — every other
middleware module is mode-agnostic.

## Deployment modes

Two modes are supported:

### standalone (default — `docker compose up`)

The backend is the **primary** enforcement layer. The browser (or any
client) talks to the backend directly through nginx's `/api/` proxy on
the same origin. Middleware stack (outermost → innermost):

  1. ProxyHeadersMiddleware     — trusts X-Forwarded-* from 127.0.0.1
  2. SecurityHeadersMiddleware  — adds CSP, X-Frame-Options, etc.
  3. SlowAPIMiddleware          — per-route rate limiting
  4. TokenValidationMiddleware  — validates Descope JWT signature via
                                  JWKS, enforces exp/iss/aud, populates
                                  request.state.claims
  5. CORSMiddleware             — same-origin in dev, env-driven in prod

`require_role` / `require_permission` dependencies in routers consume
`request.state.claims` (set by TokenValidation) to enforce per-tenant
authorization.

### gateway (`make dev-gateway` — flips DEPLOYMENT_MODE via override)

Tyk is the **primary** enforcement layer. Browser → Tyk:8080 → backend.
Tyk validates the JWT signature, expiry, issuer, and audience against
Descope's JWKS, then forwards the original `Authorization` header to
the backend. The backend's middleware stack drops TokenValidation and
SlowAPI (Tyk handles them) and adds GatewayClaimsMiddleware:

  1. ProxyHeadersMiddleware     — trusts X-Forwarded-* from 127.0.0.1
  2. SecurityHeadersMiddleware  — adds CSP, X-Frame-Options, etc.
  3. GatewayClaimsMiddleware    — base64-decodes the pre-validated JWT
                                  to populate request.state.claims;
                                  ALSO enforces exp/iss/aud as defense
                                  in depth (issue #240)
  4. CORSMiddleware             — same-origin in dev, env-driven in prod

`require_role` / `require_permission` work identically in both modes
because they only read `request.state.claims` — they don't care which
middleware populated it.

### Auth / authz boundary

  - **Authentication** (who is this?): Tyk in gateway mode,
    TokenValidationMiddleware in standalone mode. Both verify the JWT
    signature, expiry, issuer, and audience against Descope.
  - **Authorization** (what can they do?): always FastAPI. Tyk does NOT
    decode tenant claims — it just forwards the validated JWT. The
    backend's RBAC dependencies decode `dct` (current tenant) and
    `tenants[*].roles/permissions` from request.state.claims.

The `Authorization` header is intentionally NOT stripped at the Tyk
layer (`strip_auth_data: false` in tyk/apps/saas-backend.json) so the
backend can decode tenant claims after Tyk validates the signature.

### Defense in depth (issue #240)

Issue #240 documented a four-day window where `tyk/entrypoint.sh` was
silently broken and Tyk wasn't actually validating tokens, while the
backend was running in gateway mode (= signature verification skipped)
and trusting forwarded headers. To prevent a recurrence:

  - GatewayClaimsMiddleware enforces exp/iss/aud independently of Tyk,
    so a missing or misconfigured Tyk will not result in forged or
    expired tokens being trusted.
  - scripts/test-integration-{standalone,gateway}.sh assert
    `docker compose exec backend printenv DEPLOYMENT_MODE` matches
    expectations at runtime, gating CI on the mode actually flowing
    through the override.
  - test_standalone_regression.py and the standalone integration script
    prove that forged X-Tyk-Request-ID headers cannot bypass auth in
    standalone mode.

These checks are NOT a substitute for signature verification by Tyk in
gateway mode — they catch the "Tyk silently not in front" failure mode,
not the "Tyk in front but compromised" mode.

## v2 upgrade path

Replace `os.getenv("DEPLOYMENT_MODE")` with
`OpenFeature client.get_string_value("deployment_mode", "standalone")`
for hot-toggle and per-feature flag support without restarting the
backend container.
"""

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.middleware import SlowAPIMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.security import SecurityHeadersMiddleware

logger = logging.getLogger(__name__)

VALID_MODES = ("standalone", "gateway")

DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "standalone")
if DEPLOYMENT_MODE not in VALID_MODES:
    raise ValueError(f"Invalid DEPLOYMENT_MODE={DEPLOYMENT_MODE!r}. Must be one of {VALID_MODES}.")


def configure_middleware(app: FastAPI) -> None:
    """Register the middleware stack based on DEPLOYMENT_MODE.

    Middleware is added innermost-first; the last call becomes the outermost layer.

    In gateway mode, Tyk handles authentication (JWT signature, expiry, issuer)
    and rate limiting. TokenValidationMiddleware and SlowAPIMiddleware are replaced
    by GatewayClaimsMiddleware, which base64-decodes the pre-validated JWT to
    populate request.state.claims for downstream RBAC dependencies.
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

    # 2. Token handling — mode-dependent.
    #    Standalone: TokenValidationMiddleware validates JWT signatures via JWKS.
    #    Gateway: GatewayClaimsMiddleware decodes pre-validated JWT (Tyk verified).
    excluded_paths = {
        "/api/health",
        "/api/validate-id-token",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
    excluded_prefixes = {
        "/api/internal/",
    }
    if DEPLOYMENT_MODE == "standalone":
        app.add_middleware(
            TokenValidationMiddleware,
            descope_project_id=os.getenv("DESCOPE_PROJECT_ID", ""),
            excluded_paths=excluded_paths,
            excluded_prefixes=excluded_prefixes,
        )
        logger.info("Middleware included: TokenValidationMiddleware")
    else:
        app.add_middleware(
            GatewayClaimsMiddleware,
            descope_project_id=os.getenv("DESCOPE_PROJECT_ID", ""),
            excluded_paths=excluded_paths,
            excluded_prefixes=excluded_prefixes,
        )
        logger.info("Middleware included: GatewayClaimsMiddleware")

    # 3. Rate limiting — standalone only (Tyk handles rate limiting in gateway mode)
    if DEPLOYMENT_MODE == "standalone":
        app.add_middleware(SlowAPIMiddleware)
        logger.info("Middleware included: SlowAPIMiddleware")
    else:
        logger.info("Middleware excluded: SlowAPIMiddleware (gateway mode)")

    # 4. Security headers — always
    app.add_middleware(SecurityHeadersMiddleware, environment=os.getenv("ENVIRONMENT", "development"))
    logger.info("Middleware included: SecurityHeadersMiddleware")

    # 5. Proxy headers — outermost (always)
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_proxy_hosts)
    logger.info("Middleware included: ProxyHeadersMiddleware")

    logger.info("Deployment mode: %s — middleware stack configured", DEPLOYMENT_MODE)
