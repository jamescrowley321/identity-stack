"""Sync status + sync events router (DS-4.0)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.dependencies.identity import get_sync_status_service
from app.dependencies.rbac import require_role
from app.errors.problem_detail import result_to_response
from app.models.identity.sync_event import SyncEventVerb
from app.services.sync_status import SyncStatusService

router = APIRouter(tags=["Sync"])

_MAX_EVENTS_LIMIT = 200


def _parse_provider_id(value: str | None) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid UUID for provider: {value}") from exc


def _parse_verb(value: str | None) -> SyncEventVerb | None:
    if value is None or value == "":
        return None
    try:
        return SyncEventVerb(value)
    except ValueError as exc:
        allowed = ", ".join(v.value for v in SyncEventVerb)
        raise HTTPException(status_code=422, detail=f"Invalid verb '{value}'. Allowed: {allowed}") from exc


@router.get("/sync/status")
async def get_sync_status(
    request: Request,
    _operator_roles: list[str] = Depends(require_role("operator")),
    service: SyncStatusService = Depends(get_sync_status_service),
):
    """Aggregate per-provider sync state plus the most recent reconciliation timestamp."""
    result = await service.get_status()
    return result_to_response(result, request)


@router.get("/events/recent")
async def list_recent_events(
    request: Request,
    limit: int = Query(default=50, ge=1, le=_MAX_EVENTS_LIMIT),
    provider: str | None = Query(default=None),
    verb: str | None = Query(default=None),
    _operator_roles: list[str] = Depends(require_role("operator")),
    service: SyncStatusService = Depends(get_sync_status_service),
):
    """Return the most recent inbound sync events ordered by occurred_at desc."""
    provider_id = _parse_provider_id(provider)
    verb_enum = _parse_verb(verb)
    result = await service.list_events(limit=limit, provider_id=provider_id, verb=verb_enum)
    return result_to_response(result, request)
