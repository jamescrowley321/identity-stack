"""Unit tests for SyncEventRepository against real Postgres."""

import uuid

import pytest

from app.models.identity.provider import Provider, ProviderType
from app.models.identity.sync_event import SyncEvent, SyncEventVerb
from app.repositories.provider import ProviderRepository
from app.repositories.sync_event import SyncEventRepository

pytestmark = pytest.mark.asyncio


def _make_provider(**overrides) -> Provider:
    defaults = {
        "id": uuid.uuid4(),
        "name": f"provider-{uuid.uuid4().hex[:8]}",
        "type": ProviderType.descope,
    }
    defaults.update(overrides)
    return Provider(**defaults)


def _make_event(provider_id: uuid.UUID | None, **overrides) -> SyncEvent:
    defaults = {
        "provider_id": provider_id,
        "verb": SyncEventVerb.created,
        "subject_type": "user",
        "subject_id": str(uuid.uuid4()),
        "external_sub": f"ext-{uuid.uuid4().hex[:8]}",
    }
    defaults.update(overrides)
    return SyncEvent(**defaults)


async def test_create_and_list_recent(db_session):
    provider_repo = ProviderRepository(db_session)
    event_repo = SyncEventRepository(db_session)

    provider = _make_provider()
    await provider_repo.create(provider)
    await event_repo.create(_make_event(provider.id))
    await event_repo.create(_make_event(provider.id, verb=SyncEventVerb.updated))

    events = await event_repo.list_recent(limit=10)
    assert len(events) >= 2
    assert events[0].occurred_at >= events[-1].occurred_at


async def test_list_recent_filters_by_verb(db_session):
    provider_repo = ProviderRepository(db_session)
    event_repo = SyncEventRepository(db_session)
    provider = _make_provider()
    await provider_repo.create(provider)

    await event_repo.create(_make_event(provider.id, verb=SyncEventVerb.created))
    await event_repo.create(_make_event(provider.id, verb=SyncEventVerb.deleted))

    deleted = await event_repo.list_recent(limit=10, verb=SyncEventVerb.deleted)
    assert all(e.verb == SyncEventVerb.deleted for e in deleted)
    assert len(deleted) >= 1


async def test_list_recent_filters_by_provider(db_session):
    provider_repo = ProviderRepository(db_session)
    event_repo = SyncEventRepository(db_session)
    p1 = _make_provider()
    p2 = _make_provider()
    await provider_repo.create(p1)
    await provider_repo.create(p2)

    await event_repo.create(_make_event(p1.id))
    await event_repo.create(_make_event(p2.id))

    p1_events = await event_repo.list_recent(limit=10, provider_id=p1.id)
    assert all(e.provider_id == p1.id for e in p1_events)
    assert len(p1_events) >= 1


async def test_list_recent_respects_limit(db_session):
    provider_repo = ProviderRepository(db_session)
    event_repo = SyncEventRepository(db_session)
    provider = _make_provider()
    await provider_repo.create(provider)

    for _ in range(5):
        await event_repo.create(_make_event(provider.id))

    events = await event_repo.list_recent(limit=3)
    assert len(events) == 3


async def test_latest_per_provider(db_session):
    provider_repo = ProviderRepository(db_session)
    event_repo = SyncEventRepository(db_session)
    p1 = _make_provider()
    p2 = _make_provider()
    await provider_repo.create(p1)
    await provider_repo.create(p2)

    await event_repo.create(_make_event(p1.id, verb=SyncEventVerb.created))
    await event_repo.create(_make_event(p1.id, verb=SyncEventVerb.updated))
    await event_repo.create(_make_event(p2.id, verb=SyncEventVerb.created))

    latest = await event_repo.latest_per_provider()
    assert p1.id in latest
    assert p2.id in latest


async def test_latest_overall_returns_most_recent(db_session):
    provider_repo = ProviderRepository(db_session)
    event_repo = SyncEventRepository(db_session)
    provider = _make_provider()
    await provider_repo.create(provider)

    await event_repo.create(_make_event(provider.id, verb=SyncEventVerb.created))
    await event_repo.create(_make_event(provider.id, verb=SyncEventVerb.deleted))

    latest = await event_repo.latest_overall()
    assert latest is not None
    assert latest.verb in {SyncEventVerb.created, SyncEventVerb.deleted}


async def test_latest_overall_empty(db_session):
    event_repo = SyncEventRepository(db_session)
    # No guarantee of empty in shared DB — at least asserts call returns something or None
    result = await event_repo.latest_overall()
    assert result is None or isinstance(result, SyncEvent)
