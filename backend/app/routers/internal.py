"""Internal router for inbound sync endpoints.

AC-3.1.1: POST /api/internal/users/sync — Descope Flow HTTP Connector
AC-3.1.2: POST /api/internal/webhooks/descope — Descope audit webhook
AC-3.1.3: HMAC validation on webhook endpoint
AC-3.1.4: /api/internal/ prefix excluded from JWT auth
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr

from app.dependencies.identity import get_identity_resolution_service, get_inbound_sync_service
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.identity_resolution import IdentityResolutionService
from app.services.inbound_sync import InboundSyncService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Internal"])

# Read secrets once at import time; validated at startup in lifespan.
_FLOW_SYNC_SECRET = os.getenv("DESCOPE_FLOW_SYNC_SECRET", "")
_WEBHOOK_SECRET = os.getenv("DESCOPE_WEBHOOK_SECRET", "")
_IDENTITY_KEY = os.getenv("INTERNAL_IDENTITY_KEY", "")


# --- Request models ---


class FlowSyncRequest(BaseModel):
    """Payload from Descope Flow HTTP Connector."""

    user_id: str
    email: EmailStr
    name: str | None = None
    given_name: str | None = None
    family_name: str | None = None


class WebhookPayload(BaseModel):
    """Payload from Descope audit webhook."""

    event_type: str
    data: dict


# --- Auth dependency: shared secret for flow sync ---


async def verify_flow_sync_secret(
    x_flow_secret: str = Header(..., alias="X-Flow-Secret"),
) -> None:
    """Validate shared secret on flow sync requests.

    Uses DESCOPE_FLOW_SYNC_SECRET env var with timing-safe comparison.
    Missing/invalid secret → 401.
    """
    if not _FLOW_SYNC_SECRET:
        logger.error("DESCOPE_FLOW_SYNC_SECRET not configured — rejecting flow sync")
        raise HTTPException(status_code=401, detail="Flow sync secret not configured")

    if not hmac.compare_digest(_FLOW_SYNC_SECRET, x_flow_secret):
        raise HTTPException(status_code=401, detail="Invalid flow sync secret")


# --- Auth dependency: shared secret for identity resolution ---


async def verify_identity_key(
    x_identity_key: str = Header(..., alias="X-Identity-Key"),
) -> None:
    """Validate shared secret on identity resolution requests.

    Uses INTERNAL_IDENTITY_KEY env var with timing-safe comparison.
    Missing/invalid key → 401.
    """
    if not _IDENTITY_KEY:
        logger.error("INTERNAL_IDENTITY_KEY not configured — rejecting identity request")
        raise HTTPException(status_code=401, detail="Identity key not configured")

    if not hmac.compare_digest(_IDENTITY_KEY, x_identity_key):
        raise HTTPException(status_code=401, detail="Invalid identity key")


# --- Auth dependency: HMAC for webhooks ---


async def verify_hmac_signature(
    request: Request,
    x_descope_webhook_s256: str = Header(..., alias="X-Descope-Webhook-S256"),
) -> None:
    """Validate HMAC-SHA256 signature on webhook requests.

    AC-3.1.3: Uses DESCOPE_WEBHOOK_SECRET env var and timing-safe comparison.
    Invalid signature → 401 response.
    """
    if not _WEBHOOK_SECRET:
        logger.error("DESCOPE_WEBHOOK_SECRET not configured — rejecting webhook")
        raise HTTPException(status_code=401, detail="Webhook secret not configured")

    body = await request.body()
    expected = hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, x_descope_webhook_s256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


# --- Routes ---


@router.post("/internal/users/sync", dependencies=[Depends(verify_flow_sync_secret)])
@limiter.limit(RATE_LIMIT_AUTH)
async def flow_sync_user(
    request: Request,
    body: FlowSyncRequest,
    service: InboundSyncService = Depends(get_inbound_sync_service),
):
    """Receive user sync from Descope Flow HTTP Connector.

    AC-3.1.1: Creates or updates canonical user + IdP link.
    Returns 201 for new user, 200 for updated user.
    """
    result = await service.sync_user_from_flow(
        user_id=body.user_id,
        email=body.email,
        name=body.name,
        given_name=body.given_name,
        family_name=body.family_name,
    )

    status = 201 if (result.is_ok() and result.ok.get("created")) else 200
    return result_to_response(result, request, status=status)


@router.post("/internal/webhooks/descope", dependencies=[Depends(verify_hmac_signature)])
@limiter.limit(RATE_LIMIT_AUTH)
async def descope_webhook(
    request: Request,
    body: WebhookPayload,
    service: InboundSyncService = Depends(get_inbound_sync_service),
):
    """Handle Descope audit webhook events.

    AC-3.1.2: Routes event by type with idempotent processing.
    AC-3.1.3: HMAC validation via verify_hmac_signature dependency.
    """
    result = await service.process_webhook_event(
        event_type=body.event_type,
        data=body.data,
    )
    return result_to_response(result, request)


@router.get("/internal/identity", dependencies=[Depends(verify_identity_key)])
@limiter.limit(RATE_LIMIT_AUTH)
async def resolve_identity(
    request: Request,
    sub: str = Query(..., min_length=1, max_length=255, description="External subject identifier from the IdP"),
    provider: str = Query(..., min_length=1, max_length=255, description="Provider name (e.g. 'descope')"),
    service: IdentityResolutionService = Depends(get_identity_resolution_service),
):
    """Resolve canonical identity from provider + external subject.

    AC-4.3.1: Internal-only endpoint for identity resolution.
    AC-4.3.4: Shared-secret auth via X-Identity-Key header (JWT excluded on internal prefix).
    """
    result = await service.resolve(provider=provider, sub=sub)
    return result_to_response(result, request)
