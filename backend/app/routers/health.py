import os
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

DESCOPE_PROJECT_ID = os.getenv("DESCOPE_PROJECT_ID", "")
DESCOPE_BASE_URL = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")
HEALTH_CHECK_TIMEOUT = 5.0
CACHE_TTL = 30

# Simple in-memory cache for readiness results
_cache: dict = {"result": None, "timestamp": 0.0}


def _check_database() -> str:
    """Verify database connectivity with a simple query."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        return f"error: {exc}"


async def _check_descope() -> str:
    """Verify Descope API is reachable by hitting the OIDC discovery endpoint."""
    if not DESCOPE_PROJECT_ID:
        return "ok"  # No project configured — skip check
    url = f"{DESCOPE_BASE_URL}/{DESCOPE_PROJECT_ID}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return "ok"
    except Exception as exc:
        return f"error: {exc}"


async def _build_readiness_response() -> tuple[dict, int]:
    """Run dependency checks (with caching) and return (body, status_code)."""
    now = time.monotonic()
    if _cache["result"] is not None and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["result"]

    db_status = _check_database()
    descope_status = await _check_descope()

    all_ok = db_status == "ok" and descope_status == "ok"
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
