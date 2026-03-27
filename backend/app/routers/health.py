import asyncio
import os
import re
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.logging_config import get_logger
from app.middleware.rate_limit import limiter
from app.models.database import engine

logger = get_logger(__name__)
router = APIRouter()

HEALTH_CHECK_TIMEOUT = 5.0
CACHE_TTL_HEALTHY = 30
CACHE_TTL_DEGRADED = 5

# Allowlist pattern for valid Descope base URLs
_DESCOPE_URL_PATTERN = re.compile(r"^https://[a-z0-9-]+\.descope\.com$")
_DEFAULT_DESCOPE_BASE_URL = "https://api.descope.com"

# Simple in-memory cache for readiness results
_cache: dict = {"result": None, "timestamp": 0.0}
_cache_lock: asyncio.Lock | None = None

# Shared HTTP client for health checks (created lazily)
_http_client: httpx.AsyncClient | None = None


def _get_cache_lock() -> asyncio.Lock:
    """Lazily create the asyncio lock (must be in a running event loop)."""
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def _get_http_client() -> httpx.AsyncClient:
    """Return a shared HTTP client, creating it lazily."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT)
    return _http_client


def _validate_descope_base_url(url: str) -> str:
    """Validate and return a safe Descope base URL.

    Only allows URLs matching ``https://<subdomain>.descope.com``.
    Falls back to the default API URL if validation fails.
    """
    if _DESCOPE_URL_PATTERN.match(url):
        return url
    logger.warning("health.invalid_descope_base_url url=%s — falling back to default", url)
    return _DEFAULT_DESCOPE_BASE_URL


def _check_database_sync() -> str:
    """Verify database connectivity with a simple query (blocking)."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        logger.warning("health.database_check_failed: %s", exc)
        return "error"


async def _check_database() -> str:
    """Verify database connectivity without blocking the event loop."""
    return await asyncio.to_thread(_check_database_sync)


async def _check_descope() -> str:
    """Verify Descope API is reachable by hitting the OIDC discovery endpoint."""
    project_id = os.getenv("DESCOPE_PROJECT_ID", "")
    if not project_id:
        return "not_configured"
    raw_base_url = os.getenv("DESCOPE_BASE_URL", _DEFAULT_DESCOPE_BASE_URL)
    base_url = _validate_descope_base_url(raw_base_url)
    url = f"{base_url}/{project_id}/.well-known/openid-configuration"
    try:
        client = _get_http_client()
        resp = await client.get(url)
        resp.raise_for_status()
        return "ok"
    except Exception as exc:
        logger.warning("health.descope_check_failed: %s", exc)
        return "error"


async def _build_readiness_response() -> tuple[dict, int]:
    """Run dependency checks (with caching) and return (body, status_code)."""
    lock = _get_cache_lock()
    async with lock:
        now = time.monotonic()
        if _cache["result"] is not None and (now - _cache["timestamp"]) < _cache.get("ttl", CACHE_TTL_HEALTHY):
            return _cache["result"]

        db_status, descope_status = await asyncio.gather(_check_database(), _check_descope())

        all_ok = db_status == "ok" and descope_status in ("ok", "not_configured")
        status = "healthy" if all_ok else "degraded"
        status_code = 200 if all_ok else 503

        body = {
            "status": status,
            "dependencies": {
                "database": db_status,
                "descope": descope_status,
            },
        }

        if not all_ok:
            logger.warning("health.degraded database=%s descope=%s", db_status, descope_status)

        _cache["result"] = (body, status_code)
        _cache["timestamp"] = now
        _cache["ttl"] = CACHE_TTL_HEALTHY if all_ok else CACHE_TTL_DEGRADED
        return body, status_code


@router.get("/health")
@limiter.exempt
async def health(request: Request):
    """Combined health check — verifies all dependencies. Returns 503 if degraded."""
    body, status_code = await _build_readiness_response()
    return JSONResponse(content=body, status_code=status_code)


@router.get("/health/live")
@limiter.exempt
async def liveness(request: Request):
    """Liveness probe — always returns 200 if the process is running."""
    return {"status": "ok"}


@router.get("/health/ready")
@limiter.exempt
async def readiness(request: Request):
    """Readiness probe — checks database and Descope API connectivity."""
    body, status_code = await _build_readiness_response()
    return JSONResponse(content=body, status_code=status_code)
