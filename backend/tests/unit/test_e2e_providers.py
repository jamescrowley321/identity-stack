"""The shared e2e suite's providers fail a run that cannot start, never skip it (``tests/e2e/helpers/providers.py``)."""

import pytest

from tests.e2e import not_applicable as na
from tests.e2e.helpers import providers

UNREACHABLE = "http://127.0.0.1:9"


class TestDescopeCredentialsInCI:
    """The Descope run's CI check, unchanged by the move into the provider."""

    @pytest.fixture
    def descope(self):
        return providers.DescopeProvider(UNREACHABLE, UNREACHABLE)

    def test_a_missing_credential_fails_a_ci_run_naming_it(self, descope, monkeypatch):
        monkeypatch.setenv("CI", "true")
        for name in providers.DESCOPE_CI_REQUIRED_ENV:
            monkeypatch.setenv(name, "set")
        monkeypatch.delenv("DESCOPE_CLIENT_SECRET")
        monkeypatch.setenv("E2E_TEST_EMAIL", "")

        with pytest.raises(
            pytest.fail.Exception, match=r"E2E credentials missing in CI: DESCOPE_CLIENT_SECRET, E2E_TEST_EMAIL\."
        ):
            descope.check_ready(None)

    def test_every_credential_present_passes(self, descope, monkeypatch):
        monkeypatch.setenv("CI", "true")
        for name in providers.DESCOPE_CI_REQUIRED_ENV:
            monkeypatch.setenv(name, "set")

        assert descope.check_ready(None) is None

    def test_outside_ci_nothing_is_required(self, descope, monkeypatch):
        monkeypatch.delenv("CI", raising=False)
        for name in providers.DESCOPE_CI_REQUIRED_ENV:
            monkeypatch.delenv(name, raising=False)

        assert descope.check_ready(None) is None


class TestDescopeCredentialsMissing:
    @pytest.mark.parametrize(
        ("environment", "missing"),
        [
            ({}, True),
            ({"DESCOPE_MANAGEMENT_KEY": "k"}, True),
            ({"DESCOPE_MANAGEMENT_KEY": "k", "DESCOPE_CLIENT_ID": ""}, True),
            ({"DESCOPE_MANAGEMENT_KEY": "k", "DESCOPE_CLIENT_ID": "c"}, False),
            ({"E2E_PROVIDER": "keycloak"}, False),
            ({"E2E_PROVIDER": "zitadel"}, False),
        ],
    )
    def test_only_a_descope_run_skips_on_missing_descope_credentials(self, environment, missing):
        assert (
            na.descope_credentials_missing("DESCOPE_MANAGEMENT_KEY", "DESCOPE_CLIENT_ID", environment=environment)
            is missing
        )


class TestNoInstanceFails:
    def test_zitadel_without_outputs_fails_the_run(self, monkeypatch):
        monkeypatch.delenv("ZITADEL_TERRAFORM_OUTPUTS", raising=False)

        with pytest.raises(pytest.fail.Exception, match="ZITADEL_TERRAFORM_OUTPUTS is not set"):
            providers.ZitadelProvider(UNREACHABLE, UNREACHABLE).check_ready(None)

    def test_keycloak_unreachable_fails_the_run(self, monkeypatch):
        provider = providers.KeycloakProvider(UNREACHABLE, UNREACHABLE)
        monkeypatch.setattr(provider._keycloak, "KEYCLOAK_BASE_URL", UNREACHABLE)
        source = provider._keycloak.KeycloakTokenSource
        monkeypatch.setattr(provider._keycloak, "KeycloakTokenSource", lambda: source(base_url=UNREACHABLE))

        with pytest.raises(pytest.fail.Exception, match="keycloak-test is not reachable"):
            provider.check_ready(None)


def test_every_provider_has_an_implementation():
    assert set(providers.PROVIDER_CLASSES) == set(na.PROVIDERS)
    assert {name: cls.name for name, cls in providers.PROVIDER_CLASSES.items()} == {name: name for name in na.PROVIDERS}
