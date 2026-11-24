"""Which pending pushes a push retry turn pushes (``PendingPushRepository.list_due``), against the test Postgres.

A mark is due once it is ``interval`` old; after n failed pushes of its entity
since it was marked, the next retry waits ``interval * 2 ** (n - 1)`` from the
last, at most ``interval * 2 ** MAX_BACKOFF_DOUBLINGS``. A revocation (a
membership a role left, a user deleted or deactivated) is never backed off. The
marks and the failures are written directly, with the ages each case needs.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.models.identity import sync_event
from app.models.identity.sync_event import SyncEvent, SyncEventVerb
from app.models.identity.sync_ledger import PendingPush
from app.models.identity.user import User, UserStatus
from app.repositories.sync_event import SyncEventRepository
from app.repositories.sync_ledger import MAX_BACKOFF_DOUBLINGS, PendingPushRepository

INTERVAL = 60.0
KIND = "membership"


@pytest_asyncio.fixture(loop_scope="session")
async def ledger(db_session) -> PendingPushRepository:
    await db_session.execute(delete(PendingPush))
    return PendingPushRepository(db_session)


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


async def _mark(
    db_session,
    *,
    marked_ago: float,
    failures_ago: tuple[float, ...] = (),
    kind: str = KIND,
    key: str | None = None,
    snapshot: dict | None = None,
) -> str:
    key = key or f"{uuid.uuid4()}:{uuid.uuid4()}"
    db_session.add(PendingPush(kind=kind, key=key, seq=1, snapshot=snapshot, marked_at=_ago(marked_ago)))
    for ago in failures_ago:
        db_session.add(SyncEvent(verb=SyncEventVerb.failed, subject_type=kind, subject_id=key, occurred_at=_ago(ago)))
    await db_session.flush()
    return key


async def _user(db_session, status: UserStatus) -> str:
    """A user row, without the mark its insert's trigger writes."""
    user = User(email=f"{uuid.uuid4()}@backoff.test", user_name="backoff", status=status)
    db_session.add(user)
    await db_session.flush()
    await db_session.execute(delete(PendingPush).where(PendingPush.key == str(user.id)))
    return str(user.id)


# Failed just now, many times: any backed-off mark waits.
RECENT_FAILURES = tuple(float(index) for index in range(1, 6))


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


async def test_a_membership_a_role_left_is_not_backed_off(db_session, ledger):
    """Keycloak still holds the group a removed role stands for: it is retried every interval."""
    revoked = await _mark(
        db_session, marked_ago=3600, failures_ago=RECENT_FAILURES, snapshot={"role_id": str(uuid.uuid4())}
    )
    granted = await _mark(db_session, marked_ago=3600, failures_ago=RECENT_FAILURES)

    assert await _due(ledger) == {revoked}
    assert granted not in await _due(ledger)


async def test_a_deleted_or_deactivated_user_is_not_backed_off(db_session, ledger):
    deleted = await _mark(db_session, kind="user", key=str(uuid.uuid4()), marked_ago=3600, failures_ago=RECENT_FAILURES)
    deactivated = await _mark(
        db_session,
        kind="user",
        key=await _user(db_session, UserStatus.inactive),
        marked_ago=3600,
        failures_ago=RECENT_FAILURES,
    )
    active = await _mark(
        db_session,
        kind="user",
        key=await _user(db_session, UserStatus.active),
        marked_ago=3600,
        failures_ago=RECENT_FAILURES,
    )

    assert await _due(ledger) == {deleted, deactivated}
    assert active not in await _due(ledger)


async def test_a_revocation_still_waits_one_interval_after_it_is_marked(db_session, ledger):
    """An inline push may still be delivering it."""
    young = await _mark(db_session, kind="user", key=str(uuid.uuid4()), marked_ago=INTERVAL / 2)

    assert young not in await _due(ledger)


async def test_a_failure_is_timed_by_the_database_clock_not_the_worker(db_session, ledger, monkeypatch):
    """A worker clock an hour ahead does not push the failure an hour into the future."""

    class AheadClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(hours=1)

    monkeypatch.setattr(sync_event, "datetime", AheadClock)
    event = await SyncEventRepository(db_session).create_now(
        SyncEvent(verb=SyncEventVerb.failed, subject_type=KIND, subject_id="clock")
    )
    occurred, database_now = (
        await db_session.execute(select(SyncEvent.occurred_at, func.clock_timestamp()).where(SyncEvent.id == event.id))
    ).one()

    assert abs((database_now - occurred).total_seconds()) == pytest.approx(0, abs=5)
