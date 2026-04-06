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

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr

from app.dependencies.identity import get_inbound_sync_service
from app.errors.problem_detail import result_to_response
from app.services.inbound_sync import InboundSyncService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Internal"])


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


# --- HMAC validation dependency ---


async def verify_hmac_signature(
    request: Request,
    x_descope_webhook_s256: str = Header(..., alias="X-Descope-Webhook-S256"),
) -> None:
    """Validate HMAC-SHA256 signature on webhook requests.

    AC-3.1.3: Uses DESCOPE_WEBHOOK_SECRET env var and timing-safe comparison.
    Invalid signature → 401 response.
    """
    secret = os.getenv("DESCOPE_WEBHOOK_SECRET", "")
    if not secret:
        logger.error("DESCOPE_WEBHOOK_SECRET not configured — rejecting webhook")
        raise HTTPException(status_code=401, detail="Webhook secret not configured")

    body = await request.body()
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, x_descope_webhook_s256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


# --- Routes ---


@router.post("/internal/users/sync")
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
