"""KeycloakPullService — pulls the realm's admin and user events into the canonical model.

One poll reads both event stores from the stored cursors (``keycloak_pull_state``)
and applies each event, oldest first across both stores, through
``InboundSyncService.apply_keycloak_change`` (``docs/keycloak.md#event-pull``).

- **One poller at a time.** Each batch runs in one transaction that first takes
  a Postgres advisory lock (``pg_try_advisory_xact_lock``); a worker that cannot
  take it leaves the poll to the one that holds it.
- **Exactly once.** The canonical writes of a batch and the cursors that pass
  its events commit together, so a restart resumes from the last commit and
  applies nothing twice. Each event is applied in a savepoint: one that cannot be
  applied is rolled back alone, recorded as a ``failed`` sync event, and passed.
- **Keycloak unavailable.** When the realm cannot be asked (no answer or no
  token, a 5xx, a refused token, a rate limit) while an event is read, the events
  before it commit and the poll stops; the next poll reads that event again. When
  the event stores themselves cannot be read, nothing of the batch is applied.
- **The cursor.** Keycloak records event times in milliseconds and two events
  can share one, and an event can be committed a little after a later one. A
  poll therefore reads from ``OVERLAP_MS`` before the cursor and passes the
  events it has already applied by id (``*_seen``).
- **First poll.** With no stored state the cursors start at the newest event of
  each store: what happened before the pull was enabled is reconciliation's.
"""

from __future__ import annotations

import heapq
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import IdentityError, NotFound, ProviderError
from app.logbook import logbook
from app.repositories.keycloak_pull import KeycloakPullStateRepository
from app.repositories.provider import ProviderRepository
from app.services.adapters.keycloak import PROVIDER_NAME
from app.services.cache_invalidation import CacheInvalidationPublisher
from app.services.inbound_sync import InboundSyncService
from app.services.keycloak_admin import EventStream, KeycloakAdminApi, KeycloakAdminError, KeycloakAdminUnavailable
from app.services.keycloak_events import Ignored, KeycloakEventReader, RealmSnapshot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

# How far before the cursor each poll reads again, for events that share the
# cursor's millisecond or were committed after a later one.
OVERLAP_MS = 5_000

# Keycloak answers that mean "ask again later", not "this event cannot be read". A 403
# is the service account lacking a grant: asking again does not help.
_RETRY_STATUSES = frozenset({401, 408, 429})


def _transient(exc: KeycloakAdminError) -> bool:
    """Whether asking again later may work: no answer or no token, a 5xx, or a refusal of the moment.

    An answer Keycloak gave but that cannot be read is about the event, not the moment.
    """
    if isinstance(exc, KeycloakAdminUnavailable):
        return True
    return exc.status is not None and (exc.status >= 500 or exc.status in _RETRY_STATUSES)


@dataclass
class _Cursor:
    """Where one stream stands: every event before ``time`` is applied, and those in ``seen`` too."""

    time: int
    seen: dict[str, int]

    def _passes(self, event: dict) -> bool:
        return event["id"] in self.seen

    def _advance(self, event: dict) -> None:
        self.time = max(self.time, event["time"])
        self.seen[event["id"]] = event["time"]

    def _kept(self) -> dict[str, int]:
        """The applied events worth remembering: those a poll reads again, inside the overlap window."""
        floor = self.time - OVERLAP_MS
        return {event_id: time for event_id, time in self.seen.items() if time >= floor}


@dataclass
class _Batch:
    """The events of one transaction, oldest first, and whether more may follow it."""

    events: list[tuple[dict, EventStream]] = field(default_factory=list)
    more: bool = False


class KeycloakPullService:
    """Applies the realm's new events to the canonical model, from the stored cursors."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        api: KeycloakAdminApi,
        reader: KeycloakEventReader,
        inbound: InboundSyncService,
        state_repository: KeycloakPullStateRepository,
        provider_repository: ProviderRepository,
        page_size: int,
        publisher: CacheInvalidationPublisher | None = None,
    ) -> None:
        self._session = session
        self._api = api
        self._reader = reader
        self._inbound = inbound
        self._states = state_repository
        self._providers = provider_repository
        self._page_size = page_size
        self._publisher = CacheInvalidationPublisher() if publisher is None else publisher
        self._realm = api.settings.issuer

    async def poll(self) -> Result[dict, IdentityError]:
        """Apply every event recorded since the cursors, a batch (one transaction) at a time."""
        with tracer.start_as_current_span("KeycloakPullService.poll") as span:
            stats = {"applied": 0, "unchanged": 0, "skipped": 0, "ignored": 0, "own": 0, "failed": 0}
            while True:
                try:
                    batch = await self._batch(stats)
                except KeycloakAdminError as exc:
                    await self._session.rollback()
                    logger.warning(
                        "Keycloak pull: the realm could not be read (status %s); retrying next poll", exc.status
                    )
                    span.set_attribute("keycloak_pull.status", "unavailable")
                    return Error(ProviderError(message=f"Keycloak could not be read: {exc}"))
                except Exception:
                    await self._session.rollback()
                    raise
                if batch.is_error():
                    await self._session.rollback()
                    return Error(batch.error)
                if not batch.ok:
                    break
            for key, value in stats.items():
                span.set_attribute(f"keycloak_pull.{key}", value)
            return Ok(stats)

    async def _batch(self, stats: dict[str, int]) -> Result[bool, IdentityError]:
        """One transaction: lock, read, apply, advance, commit. ``Ok(True)`` when more events may wait."""
        if not await self._states.try_lock():
            await self._session.rollback()
            stats["locked_out"] = 1
            return Ok(False)
        provider = await self._providers.get_by_name(PROVIDER_NAME)
        if provider is None:
            return Error(NotFound(message=f"No {PROVIDER_NAME!r} provider row: run the Keycloak seed first"))
        state = await self._states.get_realm(self._realm)
        if state is None:
            await self._start(stats)
            return Ok(False)

        cursors = {
            EventStream.ADMIN: _Cursor(state.admin_cursor, dict(state.admin_seen)),
            EventStream.USER: _Cursor(state.user_cursor, dict(state.user_seen)),
        }
        snapshot = RealmSnapshot(organizations=dict(state.organizations), client_roles=dict(state.client_roles))
        batch = await self._read(cursors)
        if not batch.events:
            # Nothing new: no write, and the lock goes with the transaction.
            await self._session.rollback()
            return Ok(False)
        invalidations: list[dict] = []
        unavailable: KeycloakAdminError | None = None
        for event, stream in batch.events:
            try:
                await self._apply(stream, event, snapshot, provider.id, stats, invalidations)
            except KeycloakAdminError as exc:
                # Keycloak cannot be asked now: keep what is applied, and read this event again next poll.
                unavailable = exc
                break
            cursors[stream]._advance(event)
        if unavailable is None and not batch.more:
            await self._reader.refresh(snapshot)

        state.admin_cursor = cursors[EventStream.ADMIN].time
        state.admin_seen = cursors[EventStream.ADMIN]._kept()
        state.user_cursor = cursors[EventStream.USER].time
        state.user_seen = cursors[EventStream.USER]._kept()
        state.organizations = dict(snapshot.organizations)
        state.client_roles = dict(snapshot.client_roles)
        await self._states.update(state)
        await self._states.commit()
        await self._publish(invalidations)
        if unavailable is not None:
            raise unavailable
        return Ok(batch.more)

    async def _start(self, stats: dict[str, int]) -> None:
        """The first poll of a realm: the cursors at the newest events, nothing applied."""
        cursors = {}
        for stream in EventStream:
            # Newest first, a page at a time, until an event older than the overlap window.
            newest: list[dict] = []
            while True:
                page = await self._api.events(
                    stream, date_from=0, first=len(newest), max_results=self._page_size, newest=True
                )
                newest.extend(page)
                if len(page) < self._page_size or page[-1]["time"] < newest[0]["time"] - OVERLAP_MS:
                    break
            time = newest[0]["time"] if newest else 0
            cursors[stream] = (
                time,
                {event["id"]: event["time"] for event in newest if event["time"] >= time - OVERLAP_MS},
            )
        snapshot = RealmSnapshot()
        await self._reader.refresh(snapshot)
        await self._states.start(
            self._realm,
            admin=cursors[EventStream.ADMIN],
            user=cursors[EventStream.USER],
            organizations=snapshot.organizations,
            client_roles=snapshot.client_roles,
        )
        await self._states.commit()
        stats["started"] = 1
        logger.info("Keycloak pull: started for %s from the newest events", self._realm)

    async def _read(self, cursors: dict[EventStream, _Cursor]) -> _Batch:
        """The unapplied events of one page of each stream, merged oldest first (admin first on a tie).

        When a stream's page is full, the events of the other stream after that
        page's last one wait for the next batch, so the merge stays in order.
        """
        pages: dict[EventStream, list[dict]] = {}
        horizon: int | None = None
        for stream, cursor in cursors.items():
            page, full = await self._unapplied_page(stream, cursor)
            pages[stream] = page
            if full and page:
                horizon = page[-1]["time"] if horizon is None else min(horizon, page[-1]["time"])
        streams = [
            [(event, stream) for event in page if horizon is None or event["time"] <= horizon]
            for stream, page in pages.items()
        ]
        return _Batch(
            events=list(heapq.merge(*streams, key=lambda entry: entry[0]["time"])),
            more=horizon is not None,
        )

    async def _unapplied_page(self, stream: EventStream, cursor: _Cursor) -> tuple[list[dict], bool]:
        """The first page from the cursor that holds an event not yet applied, and whether it was full."""
        first = 0
        date_from = max(0, cursor.time - OVERLAP_MS)
        while True:
            page = await self._api.events(stream, date_from=date_from, first=first, max_results=self._page_size)
            fresh = [event for event in page if not cursor._passes(event)]
            full = len(page) >= self._page_size
            if fresh or not full:
                return fresh, full
            first += len(page)

    async def _apply(
        self,
        stream: EventStream,
        event: dict,
        snapshot: RealmSnapshot,
        provider_id: uuid.UUID,
        stats: dict[str, int],
        invalidations: list[dict],
    ) -> None:
        """Apply one event in a savepoint; one that cannot be applied is rolled back and recorded as failed."""
        event_id = event["id"]
        change_name = "unread"
        try:
            reading = await self._reader.read(stream, event, snapshot)
        except KeycloakAdminError as exc:
            if _transient(exc):
                raise
            await self._fail(provider_id, event_id, change_name, str(exc), stats)
            return
        if reading.change is None:
            stats["own" if reading.ignored is Ignored.OWN_PUSH else "ignored"] += 1
            logger.debug("Keycloak pull: event %s changes nothing canonical: %s", event_id, reading.ignored)
            return
        change_name = type(reading.change).__name__
        savepoint = await self._session.begin_nested()
        try:
            result = await self._inbound.apply_keycloak_change(
                reading.change, provider_id=provider_id, keycloak_event_id=event_id
            )
        except Exception as exc:
            # One event the database refuses must not hold every later one back. A rollback
            # that fails too (the connection is gone) raises, and the whole batch is retried.
            await savepoint.rollback()
            await self._fail(provider_id, event_id, change_name, type(exc).__name__, stats)
            return
        if result.is_error():
            await savepoint.rollback()
            await self._fail(provider_id, event_id, change_name, result.error.message, stats)
            return
        await savepoint.commit()
        stats[result.ok["status"]] += 1
        invalidations.extend(result.ok["invalidations"])

    async def _fail(
        self, provider_id: uuid.UUID, event_id: str, change: str, message: str, stats: dict[str, int]
    ) -> None:
        logger.warning("Keycloak pull: event %s (%s) could not be applied: %s", event_id, change, message)
        stats["failed"] += 1
        await self._inbound.record_keycloak_failure(
            provider_id=provider_id, keycloak_event_id=event_id, change=change, message=message
        )

    async def _publish(self, invalidations: list[dict]) -> None:
        """After the commit: each user's cached identity, and everything when a tenant, role or permission changed.

        Authorization never reads the cache (conventions §18), so a lost invalidation
        leaves only the identity read path stale until the cache's TTL: it is logged,
        and the committed batch stands.
        """
        try:
            await self._invalidate(invalidations)
        except Exception:
            logger.warning("Keycloak pull: a cache invalidation failed after the commit", exc_info=True)

    async def _invalidate(self, invalidations: list[dict]) -> None:
        users = {entry["entity_id"]: entry["operation"] for entry in invalidations if entry["entity_type"] == "user"}
        for user_id, operation in users.items():
            await self._publisher.publish(entity_type="user", entity_id=user_id, operation=operation)
        others = [entry for entry in invalidations if entry["entity_type"] != "user"]
        if others:
            stats = {f"{entry['entity_type']}s": 0 for entry in others}
            for entry in others:
                stats[f"{entry['entity_type']}s"] += 1
            await self._publisher.publish_batch(operation="keycloak_pull", stats=stats)
