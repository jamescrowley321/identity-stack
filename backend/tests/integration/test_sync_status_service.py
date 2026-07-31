"""Integration tests for SyncStatusService against real Postgres.

DS-4.0 backfill: aggregation logic exercised with real repositories — no mocks.
Verifies per-provider counts, status mapping, last_sync resolution, and
the global last_reconciliation timestamp against seeded data.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio

from app.models.identity.provider import Provider, ProviderType
from app.models.identity.sync_event import SyncEventVerb
from app.models.identity.user import IdPLink, User, UserStatus
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.sync_event import SyncEventRepository
from app.services.sync_status import SyncStatusService


def _build_service(db_session) -> SyncStatusService:
    return SyncStatusService(
        provider_repository=ProviderRepository(db_session),
        idp_link_repository=IdPLinkRepository(db_session),
        sync_event_repository=SyncEventRepository(db_session),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def seeded_topology(db_session):
    """Seed two providers (one active, one inactive), users, links, and events."""
    suffix = uuid.uuid4().hex[:8]

    active_provider = Provider(
        name=f"descope-active-{suffix}",
        type=ProviderType.descope,
        active=True,
    )
    inactive_provider = Provider(
        name=f"oidc-inactive-{suffix}",
        type=ProviderType.oidc,
        active=False,
    )
    db_session.add(active_provider)
    db_session.add(inactive_provider)
    await db_session.flush()

    users = []
    for n in range(3):
        u = User(
            email=f"u-{n}-{suffix}@example.com",
            user_name=f"u-{n}-{suffix}",
            status=UserStatus.active,
        )
        db_session.add(u)
        users.append(u)
    await db_session.flush()

    # 2 distinct user links to active_provider, 1 link to inactive_provider
    links = [
        IdPLink(
            user_id=users[0].id,
            provider_id=active_provider.id,
            external_sub=f"ext-active-0-{suffix}",
        ),
        IdPLink(
            user_id=users[1].id,
            provider_id=active_provider.id,
            external_sub=f"ext-active-1-{suffix}",
        ),
        IdPLink(
            user_id=users[2].id,
            provider_id=inactive_provider.id,
            external_sub=f"ext-inactive-0-{suffix}",
        ),
    ]
    for link in links:
        db_session.add(link)
    await db_session.flush()

    # Sync events: per-provider newest matters for last_sync, global newest for last_reconciliation
    base = datetime.now(timezone.utc)
    event_repo = SyncEventRepository(db_session)
    active_old = await event_repo.create(
        _build_event(active_provider.id, SyncEventVerb.created, base - timedelta(hours=2))
    )
    active_new = await event_repo.create(
        _build_event(active_provider.id, SyncEventVerb.updated, base - timedelta(minutes=10))
    )
    inactive_event = await event_repo.create(
        _build_event(inactive_provider.id, SyncEventVerb.linked, base - timedelta(hours=1))
    )
    await db_session.flush()

    return {
        "active": active_provider,
        "inactive": inactive_provider,
        "users": users,
        "links": links,
        "events": {"active_old": active_old, "active_new": active_new, "inactive": inactive_event},
    }


def _build_event(provider_id, verb, occurred_at):
    """Helper to build a SyncEvent with a controlled occurred_at."""
    from app.models.identity.sync_event import SyncEvent

    e = SyncEvent(
        provider_id=provider_id,
        verb=verb,
        subject_type="user",
        subject_id=str(uuid.uuid4()),
        external_sub=f"ext-{uuid.uuid4().hex[:8]}",
    )
    e.occurred_at = occurred_at
    return e


async def test_get_status_empty_database(db_session):
    """No providers, no events — empty providers list and null last_reconciliation."""
    svc = _build_service(db_session)
    result = await svc.get_status()
    assert result.is_ok()
    payload = result.ok
    assert payload["providers"] == []
    assert payload["last_reconciliation"] is None


async def test_get_status_aggregates_per_provider(db_session, seeded_topology):
    """Per-provider payload reports name/type/status/user_count/last_sync correctly."""
    svc = _build_service(db_session)
    result = await svc.get_status()
    assert result.is_ok()
    payload = result.ok

    by_id = {p["id"]: p for p in payload["providers"]}
    active = seeded_topology["active"]
    inactive = seeded_topology["inactive"]

    assert str(active.id) in by_id
    assert str(inactive.id) in by_id

    active_payload = by_id[str(active.id)]
    assert active_payload["name"] == active.name
    assert active_payload["type"] == "descope"
    assert active_payload["status"] == "active"
    assert active_payload["user_count"] == 2
    # last_sync resolves to the newer event for this provider
    assert active_payload["last_sync"] == seeded_topology["events"]["active_new"].occurred_at.isoformat()

    inactive_payload = by_id[str(inactive.id)]
    assert inactive_payload["status"] == "inactive"
    assert inactive_payload["user_count"] == 1
    assert inactive_payload["last_sync"] == seeded_topology["events"]["inactive"].occurred_at.isoformat()


async def test_get_status_last_reconciliation_matches_global_newest(db_session, seeded_topology):
    """last_reconciliation is the single newest event across providers."""
    svc = _build_service(db_session)
    result = await svc.get_status()
    assert result.is_ok()
    # Among seeded events, active_new is the newest (10 minutes ago vs 1h vs 2h)
    expected = seeded_topology["events"]["active_new"].occurred_at.isoformat()
    assert result.ok["last_reconciliation"] == expected


async def test_get_status_provider_with_no_events_has_null_last_sync(db_session):
    """When a provider has no events, its last_sync is None and user_count is 0."""
    suffix = uuid.uuid4().hex[:8]
    p = Provider(name=f"orphan-{suffix}", type=ProviderType.entra, active=True)
    db_session.add(p)
    await db_session.flush()

    svc = _build_service(db_session)
    result = await svc.get_status()
    assert result.is_ok()
    by_id = {prov["id"]: prov for prov in result.ok["providers"]}
    assert by_id[str(p.id)]["last_sync"] is None
    assert by_id[str(p.id)]["user_count"] == 0


async def test_list_events_orders_desc_and_respects_limit(db_session, seeded_topology):
    svc = _build_service(db_session)
    result = await svc.list_events(limit=2, provider_id=None, verb=None)
    assert result.is_ok()
    events = result.ok["events"]
    assert len(events) == 2
    assert events[0]["occurred_at"] >= events[1]["occurred_at"]


async def test_list_events_filters_by_provider(db_session, seeded_topology):
    svc = _build_service(db_session)
    inactive = seeded_topology["inactive"]
    result = await svc.list_events(limit=50, provider_id=inactive.id, verb=None)
    assert result.is_ok()
    events = result.ok["events"]
    assert len(events) == 1
    assert events[0]["provider_id"] == str(inactive.id)


async def test_list_events_filters_by_verb(db_session, seeded_topology):
    svc = _build_service(db_session)
    result = await svc.list_events(limit=50, provider_id=None, verb=SyncEventVerb.linked)
    assert result.is_ok()
    events = result.ok["events"]
    assert len(events) == 1
    assert events[0]["verb"] == "linked"


async def test_record_event_persists_and_appears_in_list(db_session, seeded_topology):
    """record_event appends a row visible via list_events."""
    svc = _build_service(db_session)
    active = seeded_topology["active"]
    new_when = datetime.now(timezone.utc) + timedelta(minutes=1)
    appended = await svc.record_event(
        provider_id=active.id,
        verb=SyncEventVerb.skipped,
        subject_type="user",
        subject_id=str(uuid.uuid4()),
        occurred_at=new_when,
    )
    assert appended.id is not None

    result = await svc.list_events(limit=10, provider_id=active.id, verb=SyncEventVerb.skipped)
    assert result.is_ok()
    events = result.ok["events"]
    assert any(e["id"] == str(appended.id) for e in events)
