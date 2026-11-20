"""The Keycloak event pull's settings: defaults, and the values that stop startup."""

import pytest

from app.dependencies.keycloak_pull import KeycloakPullNotConfigured, KeycloakPullSettings, keycloak_pull_settings

VARIABLES = ("KEYCLOAK_PULL_EVENTS", "KEYCLOAK_PULL_INTERVAL_SECONDS", "KEYCLOAK_PULL_PAGE_SIZE")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_defaults():
    assert keycloak_pull_settings() == KeycloakPullSettings(enabled=True, interval_seconds=10.0, page_size=100)


def test_set_values(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_PULL_EVENTS", "yes")
    monkeypatch.setenv("KEYCLOAK_PULL_INTERVAL_SECONDS", "2.5")
    monkeypatch.setenv("KEYCLOAK_PULL_PAGE_SIZE", "1000")

    assert keycloak_pull_settings() == KeycloakPullSettings(enabled=True, interval_seconds=2.5, page_size=1000)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KEYCLOAK_PULL_INTERVAL_SECONDS", "0"),
        ("KEYCLOAK_PULL_INTERVAL_SECONDS", "-1"),
        ("KEYCLOAK_PULL_INTERVAL_SECONDS", "nan"),
        ("KEYCLOAK_PULL_INTERVAL_SECONDS", "inf"),
        ("KEYCLOAK_PULL_INTERVAL_SECONDS", "soon"),
        ("KEYCLOAK_PULL_PAGE_SIZE", "0"),
        ("KEYCLOAK_PULL_PAGE_SIZE", "1001"),
        ("KEYCLOAK_PULL_PAGE_SIZE", "2.5"),
    ],
)
def test_a_value_out_of_range_is_refused_by_name(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(KeycloakPullNotConfigured, match=name):
        keycloak_pull_settings()


def test_with_the_pull_off_the_other_settings_are_not_read(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_PULL_EVENTS", "false")
    monkeypatch.setenv("KEYCLOAK_PULL_PAGE_SIZE", "not a number")

    assert keycloak_pull_settings().enabled is False
