"""Integration tests for SyncEventRepository against real Postgres.

DS-4.0 backfill: covers latest_per_provider, latest_overall, and list_recent
query paths against seeded data. The unit-tier counterpart in
tests/unit/repositories/ exercises the same repo but is run by `make test-unit`;
this file is exercised by `make test-integration` per the DS-4.0 scope.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio

from app.models.identity.provider import Provider, ProviderType
from app.models.identity.sync_event import SyncEvent, SyncEventVerb
from app.repositories.sync_event import SyncEventRepository


@pytest_asyncio.fixture(loop_scope="session")
async def two_providers(db_session):
    """Seed two distinct providers for cross-provider aggregation tests."""
    suffix = uuid.uuid4().hex[:8]
    p1 = Provider(name=f"provider-a-{suffix}", type=ProviderType.descope)
    p2 = Provider(name=f"provider-b-{suffix}", type=ProviderType.oidc)
    db_session.add(p1)
    db_session.add(p2)
    await db_session.flush()
    return p1, p2


def _make_event(
    *,
    provider_id: uuid.UUID | None,
    verb: SyncEventVerb = SyncEventVerb.created,
    occurred_at: datetime | None = None,
    subject_id: str | None = None,
) -> SyncEvent:
    event = SyncEvent(
        provider_id=provider_id,
        verb=verb,
        subject_type="user",
        subject_id=subject_id or str(uuid.uuid4()),
        external_sub=f"ext-{uuid.uuid4().hex[:8]}",
    )
    if occurred_at is not None:
        event.occurred_at = occurred_at
    return event


async def test_latest_per_provider_returns_one_row_per_provider(db_session, two_providers):
    """Each provider gets its own latest event — older events are excluded."""
    p1, p2 = two_providers
    repo = SyncEventRepository(db_session)

    base = datetime.now(timezone.utc)
    older_p1 = await repo.create(_make_event(provider_id=p1.id, occurred_at=base - timedelta(hours=2)))
    newer_p1 = await repo.create(
        _make_event(provider_id=p1.id, verb=SyncEventVerb.updated, occurred_at=base - timedelta(minutes=5))
    )
    only_p2 = await repo.create(
        _make_event(provider_id=p2.id, verb=SyncEventVerb.linked, occurred_at=base - timedelta(hours=1))
    )

    latest = await repo.latest_per_provider()

    assert set(latest.keys()) >= {p1.id, p2.id}
    assert latest[p1.id].id == newer_p1.id
    assert latest[p2.id].id == only_p2.id
    assert older_p1.id not in {evt.id for evt in latest.values()}


async def test_latest_per_provider_excludes_provider_id_null(db_session, two_providers):
    """Events with provider_id=NULL must not appear in the per-provider mapping."""
    p1, _ = two_providers
    repo = SyncEventRepository(db_session)

    await repo.create(_make_event(provider_id=p1.id))
    await repo.create(_make_event(provider_id=None, verb=SyncEventVerb.skipped))

    latest = await repo.latest_per_provider()
    assert all(pid is not None for pid in latest)
    assert p1.id in latest


async def test_latest_overall_returns_global_newest(db_session, two_providers):
    """latest_overall returns the single newest event across all providers."""
    p1, p2 = two_providers
    repo = SyncEventRepository(db_session)

    base = datetime.now(timezone.utc)
    await repo.create(_make_event(provider_id=p1.id, occurred_at=base - timedelta(hours=3)))
    await repo.create(_make_event(provider_id=p2.id, occurred_at=base - timedelta(hours=1)))
    newest = await repo.create(
        _make_event(provider_id=p1.id, verb=SyncEventVerb.deleted, occurred_at=base - timedelta(seconds=1))
    )

    latest = await repo.latest_overall()
    assert latest is not None
    assert latest.id == newest.id


async def test_latest_overall_with_no_events_returns_none(db_session):
    """A fresh transactional session with no events returns None."""
    repo = SyncEventRepository(db_session)
    result = await repo.latest_overall()
    assert result is None


async def test_list_recent_orders_desc_by_occurred_at(db_session, two_providers):
    p1, _ = two_providers
    repo = SyncEventRepository(db_session)

    base = datetime.now(timezone.utc)
    await repo.create(_make_event(provider_id=p1.id, occurred_at=base - timedelta(hours=2)))
    middle = await repo.create(_make_event(provider_id=p1.id, occurred_at=base - timedelta(hours=1)))
    newest = await repo.create(_make_event(provider_id=p1.id, occurred_at=base - timedelta(minutes=1)))

    events = await repo.list_recent(limit=10, provider_id=p1.id)
    assert events[0].id == newest.id
    assert events[1].id == middle.id
    for prev, curr in zip(events, events[1:], strict=False):
        assert prev.occurred_at >= curr.occurred_at


async def test_list_recent_filters_by_provider(db_session, two_providers):
    p1, p2 = two_providers
    repo = SyncEventRepository(db_session)

    await repo.create(_make_event(provider_id=p1.id))
    await repo.create(_make_event(provider_id=p2.id))
    await repo.create(_make_event(provider_id=p2.id, verb=SyncEventVerb.failed))

    p2_events = await repo.list_recent(limit=10, provider_id=p2.id)
    assert len(p2_events) == 2
    assert {e.provider_id for e in p2_events} == {p2.id}


async def test_list_recent_filters_by_verb(db_session, two_providers):
    p1, _ = two_providers
    repo = SyncEventRepository(db_session)

    await repo.create(_make_event(provider_id=p1.id, verb=SyncEventVerb.created))
    await repo.create(_make_event(provider_id=p1.id, verb=SyncEventVerb.deleted))
    await repo.create(_make_event(provider_id=p1.id, verb=SyncEventVerb.failed))

    failed_only = await repo.list_recent(limit=10, provider_id=p1.id, verb=SyncEventVerb.failed)
    assert len(failed_only) == 1
    assert failed_only[0].verb == SyncEventVerb.failed


async def test_list_recent_respects_limit(db_session, two_providers):
    p1, _ = two_providers
    repo = SyncEventRepository(db_session)

    base = datetime.now(timezone.utc)
    for i in range(7):
        await repo.create(_make_event(provider_id=p1.id, occurred_at=base - timedelta(minutes=i)))

    page = await repo.list_recent(limit=3, provider_id=p1.id)
    assert len(page) == 3
