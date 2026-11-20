"""The Keycloak push retry's interval: its default, and the values that stop startup."""

import pytest

from app.dependencies.keycloak_reconciliation import (
    RETRY_INTERVAL_VARIABLE,
    KeycloakPushRetryNotConfigured,
    keycloak_push_retry_interval,
)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv(RETRY_INTERVAL_VARIABLE, raising=False)


def test_default():
    assert keycloak_push_retry_interval() == 60.0


def test_set_value(monkeypatch):
    monkeypatch.setenv(RETRY_INTERVAL_VARIABLE, "2.5")

    assert keycloak_push_retry_interval() == 2.5


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "soon"])
def test_a_value_out_of_range_is_refused_by_name(monkeypatch, value):
    monkeypatch.setenv(RETRY_INTERVAL_VARIABLE, value)

    with pytest.raises(KeycloakPushRetryNotConfigured, match=RETRY_INTERVAL_VARIABLE):
        keycloak_push_retry_interval()
