import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
import sqlalchemy
from fastapi import FastAPI
from scalar_fastapi import get_scalar_api_reference
from slowapi.errors import RateLimitExceeded

from app.logging_config import setup_logging
from app.middleware.factory import configure_middleware
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_engine
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

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    engine = get_engine()

    # Verify database connectivity before accepting requests
    try:
        async with engine.connect() as conn:
            await conn.execute(sqlalchemy.text("SELECT 1"))
    except Exception as exc:
        logger.critical("Database connectivity check failed: %s", exc)
        raise RuntimeError("Cannot connect to database — verify DATABASE_URL and that the database is running") from exc

    init_telemetry(engine=engine)
    http_client = httpx.AsyncClient(timeout=30.0)
    init_descope_client(http_client=http_client)
    try:
        yield
    finally:
        try:
            # Run synchronous shutdown in executor to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, shutdown_telemetry)
        except Exception:
            logger.warning("Telemetry shutdown failed", exc_info=True)
        finally:
            try:
                shutdown_descope_client()
            except Exception:
                logger.warning("Descope client shutdown failed", exc_info=True)
            finally:
                try:
                    await http_client.aclose()
                except Exception:
                    logger.warning("HTTP client close failed", exc_info=True)
                finally:
                    await engine.dispose()


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
