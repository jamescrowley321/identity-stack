"""The Zitadel registration, and startup refusing a Zitadel provider that is half-configured or unsafe.

Each test starts from the local instance's complete configuration
(``docs/zitadel.md``: a plain-HTTP issuer with ``ZITADEL_REQUIRE_HTTPS=false``)
and changes one thing.
"""

import importlib

import pytest
from fastapi import FastAPI

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import (
    ProviderConfigurationError,
    ZitadelTokenBinding,
    build_provider_configs,
    configured_providers,
)
from app.middleware.zitadel import organization_id

LOCAL_ISSUER = "http://zitadel.localhost:8480"
REMOTE_ISSUER = "https://zitadel.example.test"
PROJECT_ID = "399781183140937731"
SPA_CLIENT_ID = "399781187016540163"
REDIRECT = "http://localhost:3000/signed-out"
ZITADEL = "Zitadel"
STANDALONE = "standalone"
GATEWAY = "gateway"
FACTORY_LOGGER = "app.middleware.factory"

ZITADEL_VARIABLES = (
    "ZITADEL_ISSUER_URL",
    "ZITADEL_AUDIENCE",
    "ZITADEL_CLIENT_ID",
    "ZITADEL_REQUIRE_HTTPS",
    "ZITADEL_POST_LOGOUT_REDIRECT_URI",
)
_PROVIDER_VARIABLES = (
    "DESCOPE_PROJECT_ID",
    "ORY_ISSUER_URL",
    "KEYCLOAK_ISSUER_URL",
    "FRONTEND_URL",
    *ZITADEL_VARIABLES,
)


@pytest.fixture
def complete(monkeypatch):
    """The local instance, completely configured, on its plain-HTTP issuer."""
    for name in _PROVIDER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ZITADEL_ISSUER_URL", LOCAL_ISSUER)
    monkeypatch.setenv("ZITADEL_AUDIENCE", PROJECT_ID)
    monkeypatch.setenv("ZITADEL_CLIENT_ID", SPA_CLIENT_ID)
    monkeypatch.setenv("ZITADEL_REQUIRE_HTTPS", "false")
    return monkeypatch


def _zitadel():
    (provider,) = [provider for provider in configured_providers() if provider.name == ZITADEL]
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
    def test_it_registers_the_issuer_with_the_project_audience_required(self, complete):
        provider = _zitadel()

        assert provider.accepted_issuers == frozenset({LOCAL_ISSUER})
        assert provider.discovery_issuer == LOCAL_ISSUER
        assert provider.audience == PROJECT_ID
        assert provider.require_audience is True
        assert provider.algorithms == frozenset({"RS256"})

    def test_the_client_binding_is_on(self, complete):
        assert _zitadel().token_binding == ZitadelTokenBinding(SPA_CLIENT_ID)

    def test_logout_is_rp_initiated_with_the_spas_client_id(self, complete):
        complete.setenv("ZITADEL_POST_LOGOUT_REDIRECT_URI", REDIRECT)
        provider = _zitadel()

        assert provider.logout_kind == "rp_initiated"
        assert provider.logout_client_id == SPA_CLIENT_ID
        assert provider.post_logout_redirect_uri == REDIRECT

    def test_the_redirect_defaults_to_the_frontend_login_page(self, complete):
        complete.setenv("FRONTEND_URL", "http://app.localhost:3000/")

        assert _zitadel().post_logout_redirect_uri == "http://app.localhost:3000/login"

    def test_authorization_is_canonical_and_first_login_reads_the_organization(self, complete):
        provider = _zitadel()

        assert provider.tenant_claims is None
        assert provider.descope_managed is False
        assert provider.first_login_organization is organization_id
        assert provider.profile_from_userinfo is True

    def test_the_https_switch_is_the_providers_own(self, complete):
        assert _zitadel().require_https is False

    def test_a_trailing_slash_is_dropped_because_the_token_issuer_has_none(self, complete):
        complete.setenv("ZITADEL_ISSUER_URL", f"{LOCAL_ISSUER}/")

        assert _zitadel().accepted_issuers == frozenset({LOCAL_ISSUER})

    def test_a_remote_issuer_starts_with_https_enforced(self, complete):
        complete.setenv("ZITADEL_ISSUER_URL", REMOTE_ISSUER)
        complete.delenv("ZITADEL_REQUIRE_HTTPS")

        assert _zitadel().require_https is True

    def test_without_an_issuer_nothing_is_registered(self, complete):
        complete.delenv("ZITADEL_ISSUER_URL")
        complete.delenv("ZITADEL_AUDIENCE")

        assert [provider.name for provider in configured_providers()] == ["Descope"]

    def test_the_explicit_builder_builds_the_same_provider(self, complete):
        built = build_provider_configs(
            zitadel_issuer_url=LOCAL_ISSUER,
            zitadel_audience=PROJECT_ID,
            zitadel_client_id=SPA_CLIENT_ID,
            zitadel_require_https=False,
            zitadel_post_logout_redirect_uri="http://localhost:3000/login",
        )

        assert built == configured_providers()


class TestACompleteConfigurationStarts:
    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_on_the_local_issuer_with_https_enforcement_off(self, complete, mode):
        assert [provider.name for provider in _start(complete, mode)] == ["Descope", ZITADEL]

    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_https_enforcement_off_is_logged_as_a_warning(self, complete, mode, caplog):
        caplog.set_level("WARNING", logger=FACTORY_LOGGER)

        _start(complete, mode)

        assert [r.getMessage() for r in caplog.records if r.name == FACTORY_LOGGER and ZITADEL in r.getMessage()] == [
            "HTTPS enforcement is off for provider Zitadel: its discovery document and signing keys are fetched "
            "over plain HTTP. Use this for a local issuer only."
        ]


class TestAHalfConfiguredZitadelDoesNotStart:
    CASES = [
        ({"ZITADEL_AUDIENCE": None}, "ZITADEL_AUDIENCE"),
        ({"ZITADEL_AUDIENCE": "  "}, "ZITADEL_AUDIENCE"),
        ({"ZITADEL_CLIENT_ID": None}, "ZITADEL_CLIENT_ID"),
        ({"ZITADEL_CLIENT_ID": ""}, "ZITADEL_CLIENT_ID"),
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


class TestAnHttpIssuerNeedsTheSwitchOff:
    @pytest.mark.parametrize("default", [None, "true", "on", ""])
    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_with_the_switch_at_its_default_the_backend_does_not_start(self, complete, mode, default):
        if default is None:
            complete.delenv("ZITADEL_REQUIRE_HTTPS")
        else:
            complete.setenv("ZITADEL_REQUIRE_HTTPS", default)

        with pytest.raises(ProviderConfigurationError, match="ZITADEL_REQUIRE_HTTPS"):
            _start(complete, mode)

    @pytest.mark.parametrize("issuer", ["http://127.0.0.1:8480", "http://localhost:8480", "HTTP://zitadel.localhost"])
    def test_loopback_is_no_exception(self, complete, issuer):
        complete.setenv("ZITADEL_ISSUER_URL", issuer)
        complete.delenv("ZITADEL_REQUIRE_HTTPS")

        with pytest.raises(ProviderConfigurationError, match="ZITADEL_REQUIRE_HTTPS"):
            configured_providers()

    def test_the_explicit_builder_refuses_it_too(self):
        with pytest.raises(ProviderConfigurationError, match="ZITADEL_REQUIRE_HTTPS"):
            build_provider_configs(
                zitadel_issuer_url=LOCAL_ISSUER, zitadel_audience=PROJECT_ID, zitadel_client_id=SPA_CLIENT_ID
            )
