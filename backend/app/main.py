import asyncio
import logging
import os
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
    idp_links,
    internal,
    permissions,
    protected,
    providers,
    reconciliation,
    roles,
    tenants,
    users,
)
from app.services.cache_invalidation import init_cache_publisher, set_redis_client, shutdown_cache_publisher
from app.services.descope import init_descope_client, shutdown_descope_client
from app.telemetry import init_telemetry, shutdown_telemetry

logger = logging.getLogger(__name__)


def _warn_missing_secrets() -> None:
    """Log warnings for internal endpoint secrets missing at startup."""
    import os

    if not os.getenv("DESCOPE_WEBHOOK_SECRET"):
        logger.warning("DESCOPE_WEBHOOK_SECRET not set — webhook endpoint will reject all requests")
    if not os.getenv("DESCOPE_FLOW_SYNC_SECRET"):
        logger.warning("DESCOPE_FLOW_SYNC_SECRET not set — flow sync endpoint will reject all requests")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    _warn_missing_secrets()
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

    # Redis for cache invalidation pub/sub — optional, degrades gracefully
    redis_client = None
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        from urllib.parse import urlparse

        parsed = urlparse(redis_url)
        if parsed.scheme not in ("redis", "rediss"):
            logger.warning("REDIS_URL has unsupported scheme '%s' — cache invalidation disabled", parsed.scheme)
        else:
            try:
                import redis.asyncio as aioredis

                redis_client = aioredis.from_url(redis_url)
                await redis_client.ping()
                logger.info("Redis connected for cache invalidation")
            except Exception:
                logger.warning("Redis connection failed — cache invalidation disabled", exc_info=True)
                if redis_client is not None:
                    await redis_client.aclose()
                redis_client = None
    else:
        logger.info("REDIS_URL not set — cache invalidation disabled")
    init_cache_publisher(redis_client=redis_client)
    set_redis_client(redis_client)

    # Start cache invalidation subscriber (AC-4.3.3) — background task
    subscriber_task = None
    if redis_client is not None:
        from app.services.identity_resolution import run_cache_invalidation_subscriber

        subscriber_task = asyncio.create_task(run_cache_invalidation_subscriber(redis_client))

    try:
        yield
    finally:
        # Cancel cache invalidation subscriber before closing Redis
        if subscriber_task is not None:
            subscriber_task.cancel()
            try:
                await subscriber_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Cache invalidation subscriber shutdown error", exc_info=True)
        try:
            set_redis_client(None)
            shutdown_cache_publisher()
        except Exception:
            logger.warning("Cache publisher shutdown failed", exc_info=True)
        try:
            if redis_client is not None:
                await redis_client.aclose()
        except Exception:
            logger.warning("Redis shutdown failed", exc_info=True)
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
app.include_router(internal.router, prefix="/api")
app.include_router(reconciliation.router, prefix="/api")
app.include_router(idp_links.router, prefix="/api")
app.include_router(providers.router, prefix="/api")


@app.get("/docs", include_in_schema=False)
async def scalar_docs():
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title=app.title,
    )
