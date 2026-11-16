"""Startup refuses a Keycloak provider that is half-configured or unsafe.

With ``KEYCLOAK_ISSUER_URL`` set, the backend starts only when the ``azp``
mapping, the declaration of every type, the portable permission lists and the
provider's authorization source are all there, and plain HTTP is allowed only
for a local issuer. Each test starts from the complete configuration of the
fixture realm and breaks one thing.
"""

import importlib
from dataclasses import replace

import pytest
from fastapi import FastAPI

from app.dependencies import rbac
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import (
    KeycloakTenantClaims,
    KeycloakTokenBinding,
    ProviderConfigurationError,
    configured_providers,
    descope_config,
    keycloak_config,
    ory_config,
    refuse_ambiguous_authorization_source,
)
from tests.helpers.keycloak_model import (
    MEMBER_TYPE_CLIENT,
    MODEL_VARIABLES,
    TENANT_ADMIN_TYPE_CLIENT,
    model_settings,
    set_model_environment,
)

LOCAL_ISSUER = "http://keycloak.localhost:8180/realms/identity-stack"
REMOTE_ISSUER = "https://keycloak.example.test/realms/identity-stack"
AUDIENCES = frozenset({MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT})
KEYCLOAK = "Keycloak"
STANDALONE = "standalone"
GATEWAY = "gateway"
FACTORY_LOGGER = "app.middleware.factory"

_PROVIDER_VARIABLES = (
    "DESCOPE_PROJECT_ID",
    "ORY_ISSUER_URL",
    "ORY_AUDIENCE",
    "ORY_REQUIRE_AUDIENCE",
    "ORY_CLIENT_ID",
    "ORY_POST_LOGOUT_REDIRECT_URI",
    "KEYCLOAK_ISSUER_URL",
    "KEYCLOAK_AUDIENCES",
    "KEYCLOAK_REQUIRE_HTTPS",
    "KEYCLOAK_POST_LOGOUT_REDIRECT_URI",
    *MODEL_VARIABLES,
    "FRONTEND_URL",
)


@pytest.fixture
def complete(monkeypatch):
    """The fixture realm, completely configured, on its local plain-HTTP issuer."""
    for name in _PROVIDER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", LOCAL_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCES", "member,tenant-admin")
    monkeypatch.setenv("KEYCLOAK_REQUIRE_HTTPS", "false")
    set_model_environment(monkeypatch, MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT)
    return monkeypatch


def _keycloak() -> object:
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


class TestACompleteConfigurationStarts:
    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_with_keycloak_localhost_and_https_enforcement_off(self, complete, mode):
        providers = _start(complete, mode)

        assert [provider.name for provider in providers] == ["Descope", KEYCLOAK]

    def test_the_provider_carries_the_binding_and_the_extractor_of_its_model(self, complete):
        provider = _keycloak()

        assert provider == keycloak_config(
            LOCAL_ISSUER,
            AUDIENCES,
            require_https=False,
            post_logout_redirect_uri="http://localhost:3000/login",
            **model_settings(MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT),
        )
        assert isinstance(provider.token_binding, KeycloakTokenBinding)
        assert isinstance(provider.tenant_claims, KeycloakTenantClaims)
        assert provider.token_binding.model == provider.tenant_claims.model
        assert provider.token_authorized_only is True

    def test_no_membership_lookup_is_wired(self, complete):
        assert _keycloak().sole_membership_lookup is None

    def test_a_remote_issuer_starts_with_https_enforced(self, complete):
        complete.setenv("KEYCLOAK_ISSUER_URL", REMOTE_ISSUER)
        complete.delenv("KEYCLOAK_REQUIRE_HTTPS")

        assert _keycloak().require_https is True

    def test_without_an_issuer_the_other_settings_are_not_read(self, complete):
        complete.delenv("KEYCLOAK_ISSUER_URL")
        complete.setenv("KEYCLOAK_APP_CLIENTS", "not a pair")
        complete.setenv("KEYCLOAK_PORTABLE_PERMISSIONS", "member=documents:write")

        assert [provider.name for provider in configured_providers()] == ["Descope"]


class TestAnIncompleteConfigurationIsRefused:
    """Each case leaves one question open, so no token could be decided without a guess."""

    CASES = [
        pytest.param({"KEYCLOAK_APP_CLIENTS": None}, "KEYCLOAK_APP_CLIENTS", id="no azp mapping"),
        pytest.param(
            {"KEYCLOAK_APP_CLIENTS": "member=member-app"}, "KEYCLOAK_APP_CLIENTS", id="a type with no app client"
        ),
        pytest.param({"KEYCLOAK_APP_CLIENTS": "member"}, "KEYCLOAK_APP_CLIENTS", id="an entry that is not a pair"),
        pytest.param(
            {"KEYCLOAK_APP_CLIENTS": "member=member-app,member=other,tenant-admin=tenant-admin-app"},
            "KEYCLOAK_APP_CLIENTS",
            id="two app clients for one type",
        ),
        pytest.param(
            {"KEYCLOAK_APP_CLIENTS": "member=tenant-admin,tenant-admin=tenant-admin-app"},
            "KEYCLOAK_APP_CLIENTS",
            id="an app client that is an accepted audience",
        ),
        pytest.param(
            {"KEYCLOAK_APP_CLIENTS": "member=shared-app,tenant-admin=shared-app"},
            "KEYCLOAK_APP_CLIENTS",
            id="one app client for two types",
        ),
        pytest.param(
            {"KEYCLOAK_APP_CLIENTS": "member=member-app,tenant-admin=tenant-admin-app,viewer=viewer-app"},
            "KEYCLOAK_APP_CLIENTS",
            id="an app client for a type that is no audience",
        ),
        pytest.param(
            {"KEYCLOAK_ORGANIZATION_BOUND_TYPES": None},
            "KEYCLOAK_ORGANIZATION_BOUND_TYPES",
            id="a type declared neither way",
        ),
        pytest.param(
            {"KEYCLOAK_PORTABLE_TYPES": None, "KEYCLOAK_PORTABLE_PERMISSIONS": None},
            "KEYCLOAK_PORTABLE_TYPES",
            id="another type declared neither way",
        ),
        pytest.param(
            {"KEYCLOAK_ORGANIZATION_BOUND_TYPES": "member,tenant-admin"},
            "KEYCLOAK_ORGANIZATION_BOUND_TYPES",
            id="a type declared both ways",
        ),
        pytest.param(
            {"KEYCLOAK_PORTABLE_TYPES": "member,viewer"}, "KEYCLOAK_AUDIENCES", id="a declared type that is no audience"
        ),
        pytest.param(
            {"KEYCLOAK_PORTABLE_PERMISSIONS": None},
            "KEYCLOAK_PORTABLE_PERMISSIONS",
            id="a portable type with no permission list",
        ),
        pytest.param(
            {"KEYCLOAK_PORTABLE_PERMISSIONS": "member=documents:read,member=documents:write"},
            "KEYCLOAK_PORTABLE_PERMISSIONS",
            id="a portable write permission",
        ),
        pytest.param(
            {"KEYCLOAK_PORTABLE_PERMISSIONS": "member=documents"},
            "KEYCLOAK_PORTABLE_PERMISSIONS",
            id="a portable permission with no action",
        ),
        pytest.param(
            {"KEYCLOAK_PORTABLE_PERMISSIONS": "documents:read"},
            "KEYCLOAK_PORTABLE_PERMISSIONS",
            id="a permission entry that is not a pair",
        ),
        pytest.param(
            {"KEYCLOAK_PORTABLE_PERMISSIONS": "member=documents:read,tenant-admin=settings:read"},
            "KEYCLOAK_PORTABLE_PERMISSIONS",
            id="a permission list for an organization-bound type",
        ),
        pytest.param({"KEYCLOAK_AUDIENCES": None}, "KEYCLOAK_AUDIENCES", id="no audience"),
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


class TestPlainHttpIsForALocalIssuerOnly:
    @pytest.mark.parametrize(
        "issuer",
        [
            "http://keycloak.example.test/realms/identity-stack",
            REMOTE_ISSUER,
            "http://10.0.0.5:8180/realms/identity-stack",
            "http://keycloak.localhost.example.test/realms/identity-stack",
        ],
    )
    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_https_enforcement_off_for_any_other_issuer_does_not_start(self, complete, mode, issuer):
        complete.setenv("KEYCLOAK_ISSUER_URL", issuer)

        with pytest.raises(ProviderConfigurationError, match="KEYCLOAK_REQUIRE_HTTPS"):
            _start(complete, mode)

    @pytest.mark.parametrize(
        "issuer",
        [
            LOCAL_ISSUER,
            "http://keycloak-test.localhost:18180/realms/identity-stack",
            "http://localhost:8180/realms/identity-stack",
            "http://127.0.0.1:8180/realms/identity-stack",
            "http://[::1]:8180/realms/identity-stack",
        ],
    )
    def test_loopback_and_dot_localhost_issuers_start(self, complete, issuer):
        complete.setenv("KEYCLOAK_ISSUER_URL", issuer)

        assert _keycloak().require_https is False

    def test_the_explicit_builder_refuses_it_too(self):
        with pytest.raises(ProviderConfigurationError, match="KEYCLOAK_REQUIRE_HTTPS"):
            keycloak_config(REMOTE_ISSUER, AUDIENCES, require_https=False, **model_settings(*sorted(AUDIENCES)))

    @staticmethod
    def _plain_http_warnings(caplog) -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == FACTORY_LOGGER and record.levelname == "WARNING"
        ]

    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_starting_with_https_enforcement_off_is_logged_as_a_warning(self, complete, mode, caplog):
        """A ``.localhost`` name is trusted as it is written, whatever it resolves to: the operator is told."""
        with caplog.at_level("WARNING", logger=FACTORY_LOGGER):
            _start(complete, mode)

        assert self._plain_http_warnings(caplog) == [
            "HTTPS enforcement is off for provider Keycloak: its discovery document and signing keys "
            "are fetched over plain HTTP. Use this for a local issuer only."
        ]

    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_starting_with_https_enforced_logs_no_such_warning(self, complete, mode, caplog):
        complete.setenv("KEYCLOAK_ISSUER_URL", REMOTE_ISSUER)
        complete.delenv("KEYCLOAK_REQUIRE_HTTPS")

        with caplog.at_level("WARNING", logger=FACTORY_LOGGER):
            _start(complete, mode)

        assert self._plain_http_warnings(caplog) == []


class TestTheAuthorizationSourceIsTheToken:
    """A provider in ``CANONICAL_RBAC_PROVIDERS`` is decided from the canonical model, not by its extractor."""

    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    @pytest.mark.parametrize("listed", [{"ory", "keycloak"}, {"keycloak"}])
    def test_keycloak_listed_as_a_canonical_provider_does_not_start(self, complete, mode, listed):
        complete.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset(listed))

        with pytest.raises(ProviderConfigurationError, match="CANONICAL_RBAC_PROVIDERS must not list 'Keycloak'"):
            _start(complete, mode)

    def test_the_name_is_compared_without_case(self, complete):
        with pytest.raises(ProviderConfigurationError, match="CANONICAL_RBAC_PROVIDERS"):
            refuse_ambiguous_authorization_source([_keycloak()], ["KeyCloak"])

    def test_a_provider_that_lost_its_extractor_declares_no_source(self, complete):
        unsourced = replace(_keycloak(), tenant_claims=None)

        with pytest.raises(ProviderConfigurationError, match="declares no authorization source"):
            refuse_ambiguous_authorization_source([unsourced], ["ory"])

    def test_the_default_list_starts(self, complete):
        assert refuse_ambiguous_authorization_source([_keycloak()], ["ory"]) is None

    def test_descope_and_ory_are_not_held_to_it(self):
        """Ory listed as canonical is its configuration, and Descope has its own startup check."""
        others = [descope_config("P123"), ory_config("https://ory.example.test", "api")]

        assert refuse_ambiguous_authorization_source(others, ["ory", "descope"]) is None
