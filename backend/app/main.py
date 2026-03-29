import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scalar_fastapi import get_scalar_api_reference
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.logging_config import setup_logging
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.correlation import CorrelationIdMiddleware
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.middleware.security import SecurityHeadersMiddleware
from app.models.database import create_db_and_tables
from app.routers import accesskeys, attributes, auth, health, permissions, protected, roles, tenants, users
from app.services.descope import init_descope_client, shutdown_descope_client

TRUSTED_PROXY_HOSTS = os.getenv("TRUSTED_PROXY_HOSTS", "127.0.0.1").split(",")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    create_db_and_tables()
    http_client = httpx.AsyncClient(timeout=30.0)
    init_descope_client(http_client=http_client)
    yield
    await http_client.aclose()
    shutdown_descope_client()


app = FastAPI(title="Descope SaaS Starter API", docs_url=None, redoc_url="/redoc", lifespan=lifespan)

# Rate limiter state and exception handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Middleware stack (last added = outermost, processes request first)
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
#    enforcement is via @limiter decorators on route handlers. For authenticated
#    endpoints, auth fires first (by design — rate-limit key uses the sub claim).
#    Unauthenticated endpoints (e.g. /validate-id-token) are rate-limited directly.
app.add_middleware(SlowAPIMiddleware)

# 4. Security headers — set on all responses including 429s
app.add_middleware(SecurityHeadersMiddleware, environment=os.getenv("ENVIRONMENT", "development"))

# 5. Correlation ID — generates/validates X-Correlation-ID for request tracing
app.add_middleware(CorrelationIdMiddleware)

# 6. Proxy headers — outermost, sets request.client from X-Forwarded-For
#    so rate limiting keys on real client IP, not the load balancer's
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=TRUSTED_PROXY_HOSTS)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(protected.router, prefix="/api")
app.include_router(tenants.router, prefix="/api")
app.include_router(roles.router, prefix="/api")
app.include_router(attributes.router, prefix="/api")
app.include_router(accesskeys.router, prefix="/api")
app.include_router(permissions.router, prefix="/api")
app.include_router(users.router, prefix="/api")


@app.get("/docs", include_in_schema=False)
async def scalar_docs():
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title=app.title,
    )
