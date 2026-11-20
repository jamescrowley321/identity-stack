"""The Keycloak registration, and startup refusing a Keycloak provider that is half-configured or unsafe.

Each test starts from the local realm's complete configuration (the
``backend_environment`` output of ``infra/keycloak``: a plain-HTTP
``.localhost`` issuer with ``KEYCLOAK_REQUIRE_HTTPS=false``) and changes one thing.
"""

import importlib

import pytest
from fastapi import FastAPI

from app.dependencies import rbac
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import (
    KeycloakTokenBinding,
    ProviderConfigurationError,
    build_provider_configs,
    configured_providers,
    keycloak_config,
    keycloak_organization_alias,
)
from tests.helpers.keycloak_model import API_AUDIENCE, KEYCLOAK_VARIABLES, SPA_CLIENT_ID, set_client_environment

LOCAL_ISSUER = "http://keycloak.localhost:8180/realms/identity-stack"
REMOTE_ISSUER = "https://keycloak.example.test/realms/identity-stack"
REDIRECT = "http://localhost:3000/signed-out"
KEYCLOAK = "Keycloak"
STANDALONE = "standalone"
GATEWAY = "gateway"
FACTORY_LOGGER = "app.middleware.factory"

_PROVIDER_VARIABLES = (
    "DESCOPE_PROJECT_ID",
    "ORY_ISSUER_URL",
    "ZITADEL_ISSUER_URL",
    "FRONTEND_URL",
    *KEYCLOAK_VARIABLES,
)


@pytest.fixture
def complete(monkeypatch):
    """The local realm, completely configured, on its plain-HTTP issuer."""
    for name in _PROVIDER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", LOCAL_ISSUER)
    set_client_environment(monkeypatch)
    monkeypatch.setenv("KEYCLOAK_REQUIRE_HTTPS", "false")
    return monkeypatch


def _keycloak():
    (provider,) = [provider for provider in configured_providers() if provider.name == KEYCLOAK]
    return provider


def _start(monkeypatch, mode: str) -> list:
    """Run the middleware factory, as ``app.main`` does at import, and return its provider list."""
    monkeypatch.setenv("DEPLOYMENT_MODE", mode)
    import app.middleware.factory as factory

    factory = importlib.reload(factory)
    try:
        app = FastAPI()
        factory.configure_middleware(app)
    finally:
        monkeypatch.delenv("DEPLOYMENT_MODE")
        importlib.reload(factory)
    token_middlewares = {TokenValidationMiddleware.__name__, GatewayClaimsMiddleware.__name__}
    (token_middleware,) = [m for m in app.user_middleware if m.cls.__name__ in token_middlewares]
    return token_middleware.kwargs["providers"]


class TestTheRegistration:
    def test_it_registers_the_issuer_with_the_api_audience_required(self, complete):
        provider = _keycloak()

        assert provider.accepted_issuers == frozenset({LOCAL_ISSUER})
        assert provider.disco_address == f"{LOCAL_ISSUER}/.well-known/openid-configuration"
        assert provider.audience == API_AUDIENCE
        assert provider.require_audience is True
        assert provider.algorithms == frozenset({"RS256"})

    def test_the_spa_binding_is_on(self, complete):
        assert _keycloak().token_binding == KeycloakTokenBinding(SPA_CLIENT_ID)

    def test_authorization_is_canonical_and_first_login_reads_the_organization(self, complete):
        provider = _keycloak()

        assert provider.tenant_claims is None
        assert provider.descope_managed is False
        assert provider.first_login_organization is keycloak_organization_alias
        assert provider.profile_from_userinfo is False

    def test_logout_is_rp_initiated_with_the_spas_client_id(self, complete):
        complete.setenv("KEYCLOAK_POST_LOGOUT_REDIRECT_URI", REDIRECT)
        provider = _keycloak()

        assert provider.logout_kind == "rp_initiated"
        assert provider.logout_client_id == SPA_CLIENT_ID
        assert provider.post_logout_redirect_uri == REDIRECT

    def test_the_redirect_defaults_to_the_frontend_login_page(self, complete):
        complete.setenv("FRONTEND_URL", "http://app.localhost:3000/")

        assert _keycloak().post_logout_redirect_uri == "http://app.localhost:3000/login"

    def test_the_https_switch_is_the_providers_own(self, complete):
        assert _keycloak().require_https is False

    def test_a_trailing_slash_is_dropped_because_the_token_issuer_has_none(self, complete):
        complete.setenv("KEYCLOAK_ISSUER_URL", f"{LOCAL_ISSUER}/")

        assert _keycloak().accepted_issuers == frozenset({LOCAL_ISSUER})

    def test_client_ids_keep_their_case_and_lose_surrounding_space(self, complete):
        complete.setenv("KEYCLOAK_AUDIENCE", " Identity-Stack-API ")
        complete.setenv("KEYCLOAK_CLIENT_ID", " Identity-Stack-SPA ")
        provider = _keycloak()

        assert (provider.audience, provider.logout_client_id) == ("Identity-Stack-API", "Identity-Stack-SPA")

    def test_a_remote_issuer_starts_with_https_enforced(self, complete):
        complete.setenv("KEYCLOAK_ISSUER_URL", REMOTE_ISSUER)
        complete.delenv("KEYCLOAK_REQUIRE_HTTPS")

        assert _keycloak().require_https is True

    def test_without_an_issuer_nothing_is_registered(self, complete):
        complete.delenv("KEYCLOAK_ISSUER_URL")
        complete.delenv("KEYCLOAK_AUDIENCE")

        assert [provider.name for provider in configured_providers()] == ["Descope"]

    def test_the_explicit_builder_builds_the_same_provider(self, complete):
        built = build_provider_configs(
            keycloak_issuer_url=LOCAL_ISSUER,
            keycloak_audience=API_AUDIENCE,
            keycloak_client_id=SPA_CLIENT_ID,
            keycloak_require_https=False,
            keycloak_post_logout_redirect_uri="http://localhost:3000/login",
        )

        assert built == configured_providers()


class TestACompleteConfigurationStarts:
    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_on_the_local_issuer_with_https_enforcement_off(self, complete, mode):
        assert [provider.name for provider in _start(complete, mode)] == ["Descope", KEYCLOAK]

    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_listed_in_canonical_rbac_providers(self, complete, mode):
        """Keycloak is authorized from the canonical model: listing it is the supported setup."""
        complete.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "keycloak"}))

        assert [provider.name for provider in _start(complete, mode)] == ["Descope", KEYCLOAK]

    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_https_enforcement_off_is_logged_as_a_warning(self, complete, mode, caplog):
        caplog.set_level("WARNING", logger=FACTORY_LOGGER)

        _start(complete, mode)

        assert [r.getMessage() for r in caplog.records if r.name == FACTORY_LOGGER and KEYCLOAK in r.getMessage()] == [
            "HTTPS enforcement is off for provider Keycloak: its discovery document and signing keys are fetched "
            "over plain HTTP. Use this for a local issuer only."
        ]


class TestAHalfConfiguredKeycloakDoesNotStart:
    CASES = [
        ({"KEYCLOAK_AUDIENCE": None}, "KEYCLOAK_AUDIENCE"),
        ({"KEYCLOAK_AUDIENCE": "  "}, "KEYCLOAK_AUDIENCE"),
        ({"KEYCLOAK_CLIENT_ID": None}, "KEYCLOAK_CLIENT_ID"),
        ({"KEYCLOAK_CLIENT_ID": ""}, "KEYCLOAK_CLIENT_ID"),
    ]

    @staticmethod
    def _break(monkeypatch, changes: dict) -> None:
        for name, value in changes.items():
            if value is None:
                monkeypatch.delenv(name)
            else:
                monkeypatch.setenv(name, value)

    @pytest.mark.parametrize(("changes", "named"), CASES)
    def test_the_provider_list_is_not_built(self, complete, changes, named):
        self._break(complete, changes)

        with pytest.raises(ProviderConfigurationError, match=named):
            configured_providers()

    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    @pytest.mark.parametrize(("changes", "named"), CASES)
    def test_the_backend_does_not_start(self, complete, mode, changes, named):
        self._break(complete, changes)

        with pytest.raises(ProviderConfigurationError, match=named):
            _start(complete, mode)

    @pytest.mark.parametrize(("audience", "client_id"), [("", SPA_CLIENT_ID), (API_AUDIENCE, "")])
    def test_the_config_function_refuses_it_too(self, audience, client_id):
        with pytest.raises(ProviderConfigurationError):
            keycloak_config(LOCAL_ISSUER, audience, client_id)


class TestHttpsEnforcementOffNeedsALocalIssuer:
    @pytest.mark.parametrize(
        "issuer",
        [
            REMOTE_ISSUER,
            "http://keycloak.example.test/realms/identity-stack",
            "http://10.0.0.5:8180/realms/identity-stack",
            "http://localhost.example.test/realms/identity-stack",
        ],
    )
    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_a_remote_issuer_does_not_start(self, complete, mode, issuer):
        complete.setenv("KEYCLOAK_ISSUER_URL", issuer)

        with pytest.raises(ProviderConfigurationError, match="KEYCLOAK_REQUIRE_HTTPS"):
            _start(complete, mode)

    @pytest.mark.parametrize(
        "issuer",
        [
            "http://localhost:8180/realms/identity-stack",
            "http://127.0.0.1:8180/realms/identity-stack",
            "http://[::1]:8180/realms/identity-stack",
            "http://KEYCLOAK-TEST.LOCALHOST:18180/realms/identity-stack",
        ],
    )
    def test_a_local_issuer_starts(self, complete, issuer):
        complete.setenv("KEYCLOAK_ISSUER_URL", issuer)

        assert _keycloak().require_https is False

    def test_the_explicit_builder_refuses_it_too(self):
        with pytest.raises(ProviderConfigurationError, match="KEYCLOAK_REQUIRE_HTTPS"):
            build_provider_configs(
                keycloak_issuer_url=REMOTE_ISSUER,
                keycloak_audience=API_AUDIENCE,
                keycloak_client_id=SPA_CLIENT_ID,
                keycloak_require_https=False,
            )
