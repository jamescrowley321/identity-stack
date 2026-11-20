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


async def _enter(hold) -> bool:
    async with hold() as held:
        return held
