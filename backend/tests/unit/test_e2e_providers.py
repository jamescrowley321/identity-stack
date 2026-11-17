"""The shared e2e suite's providers fail a run that cannot start, never skip it (``tests/e2e/helpers/providers.py``)."""

import base64
import json

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


@pytest.fixture
def internal_endpoint_secrets(monkeypatch):
    for name in na.INTERNAL_ENDPOINT_SECRETS:
        monkeypatch.setenv(name, "set")


@pytest.mark.usefixtures("internal_endpoint_secrets")
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


class TestInternalEndpointSecretsFailTheRun:
    """The internal-endpoint tests skip without their secrets, so a Keycloak or Zitadel run fails first."""

    @pytest.mark.parametrize("cls", [providers.KeycloakProvider, providers.ZitadelProvider])
    def test_a_missing_secret_fails_the_run_naming_it(self, cls, monkeypatch):
        for name in na.INTERNAL_ENDPOINT_SECRETS:
            monkeypatch.setenv(name, "set")
        monkeypatch.delenv("INTERNAL_IDENTITY_KEY")

        with pytest.raises(pytest.fail.Exception, match=r"unset: INTERNAL_IDENTITY_KEY\b"):
            cls(UNREACHABLE, UNREACHABLE).check_ready(None)


class TestKeycloakAdminInTheSpa:
    """The SPA is built for member-app: a page signed in as the Keycloak tenant admin cannot exist."""

    def test_an_admin_context_fails_with_the_reason(self):
        provider = providers.KeycloakProvider(UNREACHABLE, UNREACHABLE)

        with pytest.raises(pytest.fail.Exception, match="tenant-admin-app, which no SPA build serves"):
            provider.signed_in_context(None, providers.ADMIN, None)

    def test_opening_the_app_as_admin_fails_with_the_reason(self):
        provider = providers.KeycloakProvider(UNREACHABLE, UNREACHABLE)

        with pytest.raises(pytest.fail.Exception, match="tenant-admin-app, which no SPA build serves"):
            provider.open_app(None, providers.ADMIN)


def _jwt(claims: dict) -> str:
    def segment(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{segment({'alg': 'none'})}.{segment(claims)}.signature"


class TestSignedInTokens:
    """A principal's token is reused until it is about to expire, then the principal signs in again."""

    @pytest.fixture
    def clock(self):
        return {"now": 1_000.0}

    @pytest.fixture
    def tokens(self, clock):
        return providers.SignedInTokens(clock=lambda: clock["now"])

    @pytest.fixture
    def sign_ins(self, clock):
        issued: list[str] = []

        def sign_in() -> str:
            issued.append(_jwt({"exp": clock["now"] + 300}))
            return issued[-1]

        return issued, sign_in

    def test_a_fresh_token_is_reused(self, tokens, sign_ins, clock):
        issued, sign_in = sign_ins
        first = tokens.get(providers.MEMBER, sign_in)
        clock["now"] += 300 - providers.SignedInTokens.MARGIN_SECONDS - 1

        assert tokens.get(providers.MEMBER, sign_in) == first
        assert issued == [first]

    def test_a_token_about_to_expire_is_replaced(self, tokens, sign_ins, clock):
        issued, sign_in = sign_ins
        first = tokens.get(providers.MEMBER, sign_in)
        clock["now"] += 300 - providers.SignedInTokens.MARGIN_SECONDS

        second = tokens.get(providers.MEMBER, sign_in)

        assert issued == [first, second]
        assert second != first

    def test_principals_have_their_own_tokens(self, tokens, sign_ins):
        issued, sign_in = sign_ins
        member = tokens.get(providers.MEMBER, sign_in)
        admin = tokens.get(providers.ADMIN, sign_in)

        assert {providers.MEMBER: member, providers.ADMIN: admin} == {
            providers.MEMBER: issued[0],
            providers.ADMIN: issued[1],
        }

    def test_a_token_without_exp_fails(self, tokens):
        def sign_in() -> str:
            return _jwt({"sub": "someone"})

        with pytest.raises(pytest.fail.Exception, match="no exp claim"):
            tokens.get(providers.MEMBER, sign_in)
