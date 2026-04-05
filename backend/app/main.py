from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from scalar_fastapi import get_scalar_api_reference
from slowapi.errors import RateLimitExceeded

from app.logging_config import setup_logging
from app.middleware.factory import configure_middleware
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import async_engine
from app.routers import (
    accesskeys,
    attributes,
    auth,
    documents,
    fga,
    health,
    permissions,
    protected,
    roles,
    tenants,
    users,
)
from app.services.descope import init_descope_client, shutdown_descope_client
from app.telemetry import init_telemetry, shutdown_telemetry


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_telemetry(engine=async_engine)
    http_client = httpx.AsyncClient(timeout=30.0)
    init_descope_client(http_client=http_client)
    try:
        yield
    finally:
        shutdown_telemetry()
        try:
            shutdown_descope_client()
        finally:
            try:
                await http_client.aclose()
            finally:
                await async_engine.dispose()


app = FastAPI(title="Descope SaaS Starter API", docs_url=None, redoc_url="/redoc", lifespan=lifespan)

# Rate limiter state and exception handler — must remain registered even in
# gateway mode to prevent import errors from @limiter decorators on routes.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Middleware stack — configured by deployment mode (standalone vs gateway)
configure_middleware(app)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(protected.router, prefix="/api")
app.include_router(tenants.router, prefix="/api")
app.include_router(roles.router, prefix="/api")
app.include_router(attributes.router, prefix="/api")
app.include_router(accesskeys.router, prefix="/api")
app.include_router(permissions.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(documents.router, prefix="/api")
app.include_router(fga.router, prefix="/api")


@app.get("/docs", include_in_schema=False)
async def scalar_docs():
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title=app.title,
    )
