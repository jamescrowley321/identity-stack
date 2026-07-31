"""SyncStatusService — aggregates per-provider sync status and event log access."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from expression import Ok, Result
from opentelemetry import trace

from app.errors.identity import IdentityError
from app.models.identity.sync_event import SyncEvent, SyncEventVerb
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.sync_event import SyncEventRepository

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class SyncStatusService:
    """Aggregates provider state, IdP link counts, and recent sync events."""

    def __init__(
        self,
        *,
        provider_repository: ProviderRepository,
        idp_link_repository: IdPLinkRepository,
        sync_event_repository: SyncEventRepository,
    ) -> None:
        self._provider_repo = provider_repository
        self._link_repo = idp_link_repository
        self._event_repo = sync_event_repository

    async def get_status(self) -> Result[dict[str, Any], IdentityError]:
        with tracer.start_as_current_span("SyncStatusService.get_status"):
            providers = await self._provider_repo.list_all()
            counts = await self._link_repo.count_users_by_provider()
            latest_events = await self._event_repo.latest_per_provider()
            latest_overall = await self._event_repo.latest_overall()

            provider_payloads = []
            for provider in providers:
                latest = latest_events.get(provider.id)
                provider_payloads.append(
                    {
                        "id": str(provider.id),
                        "name": provider.name,
                        "type": provider.type.value,
                        "status": "active" if provider.active else "inactive",
                        "user_count": int(counts.get(provider.id, 0)),
                        "last_sync": latest.occurred_at.isoformat() if latest is not None else None,
                    }
                )

            return Ok(
                {
                    "providers": provider_payloads,
                    "last_reconciliation": (
                        latest_overall.occurred_at.isoformat() if latest_overall is not None else None
                    ),
                }
            )

    async def list_events(
        self,
        *,
        limit: int,
        provider_id: uuid.UUID | None,
        verb: SyncEventVerb | None,
    ) -> Result[dict[str, Any], IdentityError]:
        with tracer.start_as_current_span("SyncStatusService.list_events") as span:
            span.set_attribute("events.limit", limit)
            events = await self._event_repo.list_recent(limit=limit, provider_id=provider_id, verb=verb)
            return Ok({"events": [_serialise_event(e) for e in events]})

    async def record_event(
        self,
        *,
        provider_id: uuid.UUID | None,
        verb: SyncEventVerb,
        subject_type: str,
        subject_id: str = "",
        external_sub: str = "",
        detail: dict | None = None,
        occurred_at: datetime | None = None,
    ) -> SyncEvent:
        """Append a sync event. Caller is responsible for the transaction commit."""
        event = SyncEvent(
            provider_id=provider_id,
            verb=verb,
            subject_type=subject_type,
            subject_id=subject_id,
            external_sub=external_sub,
            detail=detail,
        )
        if occurred_at is not None:
            event.occurred_at = occurred_at
        return await self._event_repo.create(event)


def _serialise_event(event: SyncEvent) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "provider_id": str(event.provider_id) if event.provider_id is not None else None,
        "verb": event.verb.value,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "external_sub": event.external_sub,
        "detail": event.detail,
        "occurred_at": event.occurred_at.isoformat(),
    }
