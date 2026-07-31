"""SyncEventRepository — data access for the append-only sync_events log."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.models.identity.sync_event import SyncEvent, SyncEventVerb
from app.repositories.base import BaseRepository


class SyncEventRepository(BaseRepository[SyncEvent]):
    _model = SyncEvent

    async def list_recent(
        self,
        *,
        limit: int = 50,
        provider_id: uuid.UUID | None = None,
        verb: SyncEventVerb | None = None,
    ) -> list[SyncEvent]:
        stmt = sa.select(SyncEvent).order_by(SyncEvent.occurred_at.desc()).limit(limit)
        if provider_id is not None:
            stmt = stmt.where(SyncEvent.provider_id == provider_id)
        if verb is not None:
            stmt = stmt.where(SyncEvent.verb == verb)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def latest_per_provider(self) -> dict[uuid.UUID, SyncEvent]:
        """Return a mapping of provider_id -> most recent SyncEvent for that provider."""
        subq = (
            sa.select(
                SyncEvent.provider_id,
                sa.func.max(SyncEvent.occurred_at).label("max_occurred"),
            )
            .where(SyncEvent.provider_id.isnot(None))
            .group_by(SyncEvent.provider_id)
            .subquery()
        )
        stmt = sa.select(SyncEvent).join(
            subq,
            sa.and_(
                SyncEvent.provider_id == subq.c.provider_id,
                SyncEvent.occurred_at == subq.c.max_occurred,
            ),
        )
        result = await self._session.execute(stmt)
        return {evt.provider_id: evt for evt in result.scalars().all() if evt.provider_id is not None}

    async def latest_overall(self) -> SyncEvent | None:
        stmt = sa.select(SyncEvent).order_by(SyncEvent.occurred_at.desc()).limit(1)
        result = await self._session.execute(stmt)
        return result.scalars().first()
