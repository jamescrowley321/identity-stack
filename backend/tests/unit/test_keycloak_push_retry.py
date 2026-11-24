"""The Keycloak push retry: its interval, and the loop ``main.lifespan`` runs."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.dependencies import keycloak_reconciliation
from app.dependencies.keycloak_reconciliation import (
    RETRY_INTERVAL_VARIABLE,
    KeycloakPushRetryNotConfigured,
    keycloak_push_retry_interval,
    run_keycloak_push_retry,
)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv(RETRY_INTERVAL_VARIABLE, raising=False)


def test_default():
    assert keycloak_push_retry_interval() == 60.0


def test_set_value(monkeypatch):
    monkeypatch.setenv(RETRY_INTERVAL_VARIABLE, "2.5")

    assert keycloak_push_retry_interval() == 2.5


def test_the_floor_is_accepted(monkeypatch):
    monkeypatch.setenv(RETRY_INTERVAL_VARIABLE, "1")

    assert keycloak_push_retry_interval() == 1.0


@pytest.mark.parametrize("value", ["0", "-1", "0.999", "0.001", "nan", "inf", "soon"])
def test_a_value_out_of_range_is_refused_by_name(monkeypatch, value):
    monkeypatch.setenv(RETRY_INTERVAL_VARIABLE, value)

    with pytest.raises(KeycloakPushRetryNotConfigured, match=RETRY_INTERVAL_VARIABLE):
        keycloak_push_retry_interval()


class TestTheLoop:
    @pytest.fixture
    def turns(self, monkeypatch) -> list[tuple]:
        """Each turn's arguments; the first turn raises, as a database or Keycloak failure would."""
        seen: list[tuple] = []

        @asynccontextmanager
        async def session():
            yield "session"

        async def turn(api, session_, hold, interval):
            seen.append((api, session_, hold, interval))
            if len(seen) == 1:
                raise RuntimeError("the database went away")

        monkeypatch.setattr(keycloak_reconciliation, "get_session_factory", lambda: session)
        monkeypatch.setattr(keycloak_reconciliation, "retry_keycloak_pushes", turn)
        return seen

    async def test_a_failed_turn_is_logged_and_the_next_one_runs(self, turns, caplog):
        hold = object()
        task = asyncio.create_task(run_keycloak_push_retry("api", 0.01, hold))
        while len(turns) < 2:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert turns[:2] == [("api", "session", hold, 0.01)] * 2
        assert "Keycloak push retry failed" in caplog.text

    async def test_it_ends_when_cancelled(self, turns):
        task = asyncio.create_task(run_keycloak_push_retry("api", 0.01, object()))
        while not turns:
            await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
