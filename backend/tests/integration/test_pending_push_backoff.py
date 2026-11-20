"""Which pending pushes a push retry turn pushes (``PendingPushRepository.list_due``), against the test Postgres.

A mark is due once it is ``interval`` old; after n failed pushes of its entity
since it was marked, the next retry waits ``interval * 2 ** (n - 1)`` from the
last, at most ``interval * 2 ** MAX_BACKOFF_DOUBLINGS``. The marks and the
failures are written directly, with the ages each case needs.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import delete

from app.models.identity.sync_event import SyncEvent, SyncEventVerb
from app.models.identity.sync_ledger import PendingPush
from app.repositories.sync_ledger import MAX_BACKOFF_DOUBLINGS, PendingPushRepository

INTERVAL = 60.0
KIND = "membership"


@pytest_asyncio.fixture(loop_scope="session")
async def ledger(db_session) -> PendingPushRepository:
    await db_session.execute(delete(PendingPush))
    return PendingPushRepository(db_session)


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


async def _mark(db_session, *, marked_ago: float, failures_ago: tuple[float, ...] = ()) -> str:
    key = f"{uuid.uuid4()}:{uuid.uuid4()}"
    db_session.add(PendingPush(kind=KIND, key=key, seq=1, marked_at=_ago(marked_ago)))
    for ago in failures_ago:
        db_session.add(SyncEvent(verb=SyncEventVerb.failed, subject_type=KIND, subject_id=key, occurred_at=_ago(ago)))
    await db_session.flush()
    return key


async def _due(ledger: PendingPushRepository) -> set[str]:
    return {mark.key for mark in await ledger.list_due(INTERVAL)}


async def test_a_mark_younger_than_the_interval_is_not_due(db_session, ledger):
    """An inline push may still be delivering it."""
    young = await _mark(db_session, marked_ago=INTERVAL / 2)
    old = await _mark(db_session, marked_ago=INTERVAL * 2)

    assert await _due(ledger) == {old}
    assert young not in await _due(ledger)


async def test_one_failure_waits_one_interval(db_session, ledger):
    waiting = await _mark(db_session, marked_ago=600, failures_ago=(INTERVAL / 2,))
    waited = await _mark(db_session, marked_ago=600, failures_ago=(INTERVAL * 1.5,))

    assert await _due(ledger) == {waited}
    assert waiting not in await _due(ledger)


async def test_each_failure_doubles_the_wait(db_session, ledger):
    """Three failures: four intervals from the last."""
    waiting = await _mark(db_session, marked_ago=3600, failures_ago=(INTERVAL * 3.5, 1800, 1700))
    waited = await _mark(db_session, marked_ago=3600, failures_ago=(INTERVAL * 4.5, 1800, 1700))

    assert await _due(ledger) == {waited}
    assert waiting not in await _due(ledger)


async def test_the_wait_stops_doubling(db_session, ledger):
    cap = INTERVAL * 2**MAX_BACKOFF_DOUBLINGS
    many = tuple(cap + 100 + index for index in range(MAX_BACKOFF_DOUBLINGS + 5))
    capped = await _mark(db_session, marked_ago=cap * 3, failures_ago=many)

    assert await _due(ledger) == {capped}


async def test_failures_before_the_mark_do_not_count(db_session, ledger):
    """A canonical change since the failures marks it again: it is retried one interval after that change."""
    remarked = await _mark(db_session, marked_ago=INTERVAL * 2, failures_ago=(INTERVAL * 3, INTERVAL * 4))

    assert await _due(ledger) == {remarked}
