import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.logging_config import get_logger, setup_logging
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.correlation import CorrelationIdMiddleware
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.middleware.security import SecurityHeadersMiddleware
from app.models.database import create_db_and_tables
from app.routers import accesskeys, attributes, auth, health, protected, roles, tenants, users

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("Starting Descope SaaS Starter API")
    create_db_and_tables()
    yield


app = FastAPI(title="Descope SaaS Starter API", lifespan=lifespan)

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
    excluded_paths={
        "/api/health",
        "/api/health/live",
        "/api/health/ready",
        "/api/validate-id-token",
        "/docs",
        "/openapi.json",
    },
)

# 3. Rate limiting — before auth so brute-force attempts are caught
app.add_middleware(SlowAPIMiddleware)

# 4. Security headers
app.add_middleware(SecurityHeadersMiddleware, environment=os.getenv("ENVIRONMENT", "development"))

# 5. Correlation ID — outermost so all middleware and handlers have access
app.add_middleware(CorrelationIdMiddleware)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(protected.router, prefix="/api")
app.include_router(tenants.router, prefix="/api")
app.include_router(roles.router, prefix="/api")
app.include_router(attributes.router, prefix="/api")
app.include_router(accesskeys.router, prefix="/api")
app.include_router(users.router, prefix="/api")
