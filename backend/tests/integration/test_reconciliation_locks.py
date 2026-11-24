"""The session locks that keep pending pushes from overlapping, against the test Postgres.

A reconciliation pass (``hold_reconciliation_pass``) and the Keycloak push retry
(``hold_push_retry``) both push pending pushes; two pushes of one entity from reads
of different ages must never overlap. A pass waits for a retry turn in progress,
a retry turn skips while a pass pushes, and a second pass is refused.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.dependencies import identity
from app.dependencies.identity import hold_push_retry, hold_reconciliation_pass
from app.models.database import get_engine, reset_engine


@pytest_asyncio.fixture(loop_scope="session")
async def engine(monkeypatch, postgres_url) -> AsyncIterator[None]:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    reset_engine()
    yield
    await get_engine().dispose()
    reset_engine()


async def test_a_pass_waits_for_a_push_retry_turn(engine):
    async with hold_push_retry() as retrying:
        assert retrying is True
        waiting = asyncio.create_task(_enter(hold_reconciliation_pass))
        await asyncio.sleep(0.5)
        assert not waiting.done()
    assert await asyncio.wait_for(waiting, timeout=5) is True


async def test_a_pass_gives_up_on_a_push_retry_turn_that_outlasts_its_wait(engine, monkeypatch):
    """Answered as a pass already running (409), not left waiting; the pass lock is released."""
    monkeypatch.setattr(identity, "PASS_WAITS_FOR_PUSH_SECONDS", 0.3)
    async with hold_push_retry() as retrying:
        assert retrying is True
        assert await asyncio.wait_for(_enter(hold_reconciliation_pass), timeout=5) is False
    assert await _enter(hold_reconciliation_pass) is True


async def test_a_lock_wait_cancelled_leaves_no_lock_behind(engine):
    async with hold_push_retry():
        waiting = asyncio.create_task(_enter(hold_reconciliation_pass))
        await asyncio.sleep(0.3)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    assert await _enter(hold_reconciliation_pass) is True


async def test_a_push_retry_turn_skips_while_a_pass_pushes(engine):
    async with hold_reconciliation_pass() as passing:
        assert passing is True
        assert await _enter(hold_push_retry) is False
    assert await _enter(hold_push_retry) is True


async def test_a_second_pass_is_refused(engine):
    async with hold_reconciliation_pass() as passing:
        assert passing is True
        assert await _enter(hold_reconciliation_pass) is False
    assert await _enter(hold_reconciliation_pass) is True


UNLOCK = identity._UNLOCK


@pytest.fixture
def invalidated(monkeypatch) -> list[AsyncConnection]:
    """Each connection the locks drop instead of returning to the pool."""
    dropped: list[AsyncConnection] = []
    invalidate = AsyncConnection.invalidate

    async def spy(self, exception=None):
        dropped.append(self)
        await invalidate(self, exception)

    monkeypatch.setattr(AsyncConnection, "invalidate", spy)
    return dropped


async def test_a_release_that_fails_drops_the_connection_and_its_lock(engine, monkeypatch, invalidated):
    """Not raised from the holder's exit; closing the connection frees the lock the release did not."""
    monkeypatch.setattr(identity, "_UNLOCK", text("SELECT pg_advisory_unlock_no_such_function(:lock_id)"))

    assert await _enter(hold_push_retry) is True

    assert len(invalidated) == 1
    monkeypatch.setattr(identity, "_UNLOCK", UNLOCK)
    assert await _enter_soon(hold_push_retry) is True


async def test_a_release_cancelled_drops_the_connection_and_is_raised(engine, monkeypatch, invalidated):
    monkeypatch.setattr(identity, "_UNLOCK", text("SELECT pg_sleep(2), pg_advisory_unlock(:lock_id)"))
    releasing = asyncio.create_task(_enter(hold_push_retry))
    await asyncio.sleep(0.5)

    releasing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await releasing

    assert len(invalidated) == 1
    monkeypatch.setattr(identity, "_UNLOCK", UNLOCK)
    assert await _enter_soon(hold_push_retry) is True


async def _enter_soon(hold, within: float = 10.0) -> bool:
    """Whether ``hold`` is taken within ``within`` seconds: a dropped connection's backend may take a moment to end."""
    deadline = asyncio.get_running_loop().time() + within
    while not await _enter(hold):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.2)
    return True


async def _enter(hold) -> bool:
    async with hold() as held:
        return held
