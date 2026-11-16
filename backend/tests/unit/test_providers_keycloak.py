"""The Keycloak provider config, the shared provider builder and what the middlewares do with them."""

import base64
import importlib
import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import (
    ProviderConfigurationError,
    algorithm_rejected,
    audience_rejected,
    build_provider_configs,
    configured_providers,
    descope_config,
    keycloak_config,
    ory_config,
)
from tests.helpers.keycloak_model import MODEL_VARIABLES, builder_settings, model_settings, set_model_environment

ISSUER = "http://keycloak.example.test:8180/realms/identity-stack"
MEMBER = "member"
TENANT_ADMIN = "tenant-admin"
APP_CLIENT = "member-app"
AUDIENCES = frozenset({MEMBER, TENANT_ADMIN})
# The token model that goes with ``AUDIENCES``: startup refuses a Keycloak provider without one.
MODEL = model_settings(MEMBER, TENANT_ADMIN)
# Plain HTTP may be allowed only for an issuer on loopback or a ``.localhost`` name.
LOCAL_ISSUER = "http://keycloak.localhost:8180/realms/identity-stack"
ORY_ISSUER = "https://ory.example.test"
PROBE = "/api/probe"
UNIFORM_401 = {"detail": "Invalid or expired token"}

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


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _jwt(claims: dict, *, header: object = None) -> str:
    """An unsigned JWT. The standalone tests patch ``validate_token``; the gateway reads it as is."""
    return f"{_segment({'alg': 'RS256'} if header is None else header)}.{_segment(claims)}.sig"


def _claims(**overrides) -> dict:
    claims = {"iss": ISSUER, "sub": "kc-user", "aud": MEMBER, "azp": APP_CLIENT, "exp": int(time.time()) + 300}
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not None}


def _keycloak(**overrides):
    return replace(keycloak_config(ISSUER, AUDIENCES, **MODEL), **overrides)


def _provider_name(provider) -> str:
    return provider.name


def _client(middleware, providers) -> TestClient:
    app = FastAPI()

    @app.get(PROBE)
    async def probe(request: Request):
        return {"sub": request.state.claims["sub"], "provider": request.state.principal.identity.authentication_type}

    app.add_middleware(middleware, providers=providers)
    return TestClient(app)


def _get(client: TestClient, token: str):
    return client.get(PROBE, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def clean_env(monkeypatch):
    for name in _PROVIDER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestKeycloakConfig:
    def test_issuer_discovery_and_logout_kind(self):
        provider = keycloak_config(ISSUER, AUDIENCES, **MODEL)

        assert provider.name == "Keycloak"
        assert provider.accepted_issuers == frozenset({ISSUER})
        assert provider.disco_address == f"{ISSUER}/.well-known/openid-configuration"
        assert provider.logout_kind == "rp_initiated"

    def test_trailing_slash_is_dropped_because_the_token_issuer_has_none(self):
        assert keycloak_config(f"{ISSUER}/", AUDIENCES, **MODEL).accepted_issuers == frozenset({ISSUER})

    def test_audience_is_the_set_and_is_required(self):
        provider = keycloak_config(ISSUER, AUDIENCES, **MODEL)

        assert provider.audiences == AUDIENCES
        assert provider.require_audience is True

    def test_rs256_only(self):
        assert keycloak_config(ISSUER, AUDIENCES, **MODEL).algorithms == frozenset({"RS256"})

    def test_https_is_required_unless_switched_off(self):
        assert keycloak_config(ISSUER, AUDIENCES, **MODEL).require_https is True
        assert keycloak_config(LOCAL_ISSUER, AUDIENCES, require_https=False, **MODEL).require_https is False

    def test_logout_client_is_the_tokens_azp(self):
        provider = keycloak_config(
            ISSUER, AUDIENCES, post_logout_redirect_uri="https://app.example.test/login", **MODEL
        )

        assert provider.logout_client_id is None
        assert provider.logout_client_id_claim == "azp"
        assert provider.post_logout_redirect_uri == "https://app.example.test/login"

    @pytest.mark.parametrize("audiences", [frozenset(), frozenset({""}), frozenset({"  "})])
    def test_no_usable_audience_is_refused(self, audiences):
        with pytest.raises(ProviderConfigurationError, match="KEYCLOAK_AUDIENCES"):
            keycloak_config(ISSUER, audiences)

    def test_other_providers_require_https_and_take_one_audience(self):
        for provider in (descope_config("P123"), ory_config(ORY_ISSUER, "api")):
            assert provider.require_https is True
            assert provider.audiences == frozenset()

    def test_every_provider_accepts_rs256_only(self):
        providers = {
            provider.name: provider.algorithms
            for provider in (descope_config("P123"), ory_config(ORY_ISSUER, "api"), keycloak_config(ISSUER, AUDIENCES))
        }

        assert providers == {
            "Descope": frozenset({"RS256"}),
            "Ory": frozenset({"RS256"}),
            "Keycloak": frozenset({"RS256"}),
        }


class TestBuildProviderConfigs:
    def test_keycloak_is_appended_when_an_issuer_is_configured(self):
        providers = build_provider_configs(
            descope_project_id="P123",
            ory_issuer_url=ORY_ISSUER,
            ory_audience="api",
            keycloak_issuer_url=ISSUER,
            keycloak_audiences=AUDIENCES,
            **builder_settings(MEMBER, TENANT_ADMIN),
        )

        assert [provider.name for provider in providers] == ["Descope", "Ory", "Keycloak"]

    def test_no_keycloak_without_an_issuer(self):
        providers = build_provider_configs(descope_project_id="P123", keycloak_audiences=AUDIENCES)

        assert [provider.name for provider in providers] == ["Descope"]

    def test_issuer_without_audience_fails(self):
        with pytest.raises(ProviderConfigurationError, match="KEYCLOAK_AUDIENCES"):
            build_provider_configs(descope_project_id="P123", keycloak_issuer_url=ISSUER)

    def test_the_error_is_a_value_error_like_orys(self):
        assert issubclass(ProviderConfigurationError, ValueError)


class TestConfiguredProviders:
    def test_descope_only_by_default(self, clean_env):
        clean_env.setenv("DESCOPE_PROJECT_ID", "P123")

        assert configured_providers() == [descope_config("P123")]

    def test_ory_settings_come_from_the_environment(self, clean_env):
        clean_env.setenv("ORY_ISSUER_URL", ORY_ISSUER)
        clean_env.setenv("ORY_AUDIENCE", "api")
        clean_env.setenv("ORY_CLIENT_ID", "spa")
        clean_env.setenv("ORY_POST_LOGOUT_REDIRECT_URI", "https://app.example.test/out")

        providers = {provider.name: provider for provider in configured_providers()}

        assert providers["Ory"] == ory_config(
            ORY_ISSUER, "api", client_id="spa", post_logout_redirect_uri="https://app.example.test/out"
        )
        assert providers["Ory"].logout_client_id == "spa"

    def test_ory_without_audience_still_fails(self, clean_env):
        clean_env.setenv("ORY_ISSUER_URL", ORY_ISSUER)

        with pytest.raises(ValueError, match="ORY_AUDIENCE"):
            configured_providers()

    def test_keycloak_settings_come_from_the_environment(self, clean_env):
        clean_env.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
        clean_env.setenv("KEYCLOAK_AUDIENCES", " member , tenant-admin ,,")
        set_model_environment(clean_env, MEMBER, TENANT_ADMIN)
        clean_env.setenv("KEYCLOAK_POST_LOGOUT_REDIRECT_URI", "https://app.example.test/login")

        providers = {provider.name: provider for provider in configured_providers()}

        assert providers["Keycloak"] == keycloak_config(
            ISSUER, AUDIENCES, post_logout_redirect_uri="https://app.example.test/login", **MODEL
        )

    def test_audience_client_ids_keep_their_case(self, clean_env):
        clean_env.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
        clean_env.setenv("KEYCLOAK_AUDIENCES", "Member")
        clean_env.setenv("KEYCLOAK_APP_CLIENTS", "Member=Member-App")
        clean_env.setenv("KEYCLOAK_PORTABLE_TYPES", "Member")
        clean_env.setenv("KEYCLOAK_PORTABLE_PERMISSIONS", "Member=documents:read")

        providers = {provider.name: provider for provider in configured_providers()}

        assert providers["Keycloak"].audiences == frozenset({"Member"})

    def test_post_logout_redirect_defaults_to_the_frontend_login_page(self, clean_env):
        clean_env.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
        clean_env.setenv("KEYCLOAK_AUDIENCES", MEMBER)
        set_model_environment(clean_env, MEMBER)
        clean_env.setenv("FRONTEND_URL", "https://app.example.test/")

        providers = {provider.name: provider for provider in configured_providers()}

        assert providers["Keycloak"].post_logout_redirect_uri == "https://app.example.test/login"

    @pytest.mark.parametrize("audiences", [None, "", " , "])
    def test_issuer_without_audience_fails(self, clean_env, audiences):
        clean_env.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
        if audiences is not None:
            clean_env.setenv("KEYCLOAK_AUDIENCES", audiences)

        with pytest.raises(ProviderConfigurationError, match="KEYCLOAK_AUDIENCES"):
            configured_providers()

    @pytest.mark.parametrize(
        ("value", "required"),
        [(None, True), ("true", True), ("", True), ("off", True), ("flase", True), ("false", False), ("0", False)],
    )
    def test_https_switch_is_on_unless_explicitly_off(self, clean_env, value, required):
        clean_env.setenv("KEYCLOAK_ISSUER_URL", LOCAL_ISSUER)
        clean_env.setenv("KEYCLOAK_AUDIENCES", MEMBER)
        set_model_environment(clean_env, MEMBER)
        if value is not None:
            clean_env.setenv("KEYCLOAK_REQUIRE_HTTPS", value)

        providers = {provider.name: provider for provider in configured_providers()}

        assert providers["Keycloak"].require_https is required


class TestAudienceSet:
    @pytest.mark.parametrize("aud", [MEMBER, TENANT_ADMIN, [MEMBER], ["account", TENANT_ADMIN]])
    def test_an_audience_in_the_set_is_accepted(self, aud):
        assert audience_rejected({"aud": aud}, _keycloak()) is False

    @pytest.mark.parametrize(
        "aud", ["account", "", "member-app", [], ["account"], [["member"]], None, 7, {"member": 1}]
    )
    def test_an_audience_outside_the_set_is_rejected(self, aud):
        assert audience_rejected({"aud": aud}, _keycloak()) is True

    def test_a_missing_audience_is_rejected(self):
        assert audience_rejected({}, _keycloak()) is True

    def test_a_required_audience_with_nothing_configured_rejects_everything(self):
        provider = ory_config(ORY_ISSUER, audience=None, require_audience=True)

        assert audience_rejected({"aud": ""}, provider) is True
        assert audience_rejected({"aud": "anything"}, provider) is True


class TestAlgorithmRejected:
    def test_rs256_is_accepted(self):
        assert algorithm_rejected(_jwt({}, header={"alg": "RS256"}), _keycloak()) is False

    @pytest.mark.parametrize("alg", ["ES256", "PS256", "HS256", "none", "rs256", "", None, 7, ["RS256"]])
    def test_any_other_algorithm_is_rejected(self, alg):
        assert algorithm_rejected(_jwt({}, header={"alg": alg}), _keycloak()) is True

    @pytest.mark.parametrize(
        "token",
        ["", "opaque", "a.b", "!!!.e30.sig", f"{_segment(['RS256'])}.e30.sig", f"{_segment({})}.e30.sig"],
    )
    def test_an_unreadable_header_is_rejected(self, token):
        assert algorithm_rejected(token, _keycloak()) is True

    @pytest.mark.parametrize("provider", [descope_config("P123"), ory_config(ORY_ISSUER, "api")], ids=_provider_name)
    def test_descope_and_ory_get_the_same_rule(self, provider):
        assert algorithm_rejected(_jwt({}, header={"alg": "RS256"}), provider) is False
        assert algorithm_rejected(_jwt({}, header={"alg": "ES256"}), provider) is True
        assert algorithm_rejected(_jwt({}, header={"alg": "none"}), provider) is True
        assert algorithm_rejected("opaque", provider) is True

    def test_a_provider_that_pins_nothing_rejects_nothing(self):
        unpinned = _keycloak(algorithms=frozenset())

        assert algorithm_rejected(_jwt({}, header={"alg": "ES256"}), unpinned) is False
        assert algorithm_rejected("opaque", unpinned) is False


class TestTokenValidationMiddleware:
    def _providers(self, **overrides):
        return [descope_config("P123"), _keycloak(**overrides)]

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_valid_token_authenticates_as_keycloak(self, validate):
        validate.return_value = _claims()

        response = _get(_client(TokenValidationMiddleware, self._providers()), _jwt(_claims()))

        assert response.status_code == 200
        assert response.json() == {"sub": "kc-user", "provider": "Keycloak"}

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_library_gets_the_https_switch_and_the_algorithm(self, validate):
        validate.return_value = _claims()

        _get(_client(TokenValidationMiddleware, self._providers()), _jwt(_claims()))
        strict = validate.await_args.kwargs["token_validation_config"]
        _get(_client(TokenValidationMiddleware, self._providers(require_https=False)), _jwt(_claims()))
        relaxed = validate.await_args.kwargs["token_validation_config"]

        assert strict.require_https is True
        assert relaxed.require_https is False
        assert strict.algorithms == ["RS256"]
        assert validate.await_args.kwargs["disco_doc_address"] == f"{ISSUER}/.well-known/openid-configuration"

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_library_gets_https_and_the_algorithm_for_ory(self, validate):
        ory_claims = {"iss": ORY_ISSUER, "sub": "ory-user", "aud": "api"}
        validate.return_value = ory_claims
        client = _client(TokenValidationMiddleware, [descope_config("P123"), ory_config(ORY_ISSUER, "api")])

        assert _get(client, _jwt(ory_claims)).status_code == 200

        config = validate.await_args.kwargs["token_validation_config"]
        assert config.require_https is True
        assert config.algorithms == ["RS256"]

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_library_gets_https_and_the_algorithm_for_descope(self, validate):
        descope_claims = {"iss": "https://api.descope.com/P123", "sub": "descope-user", "aud": "P123"}
        validate.return_value = descope_claims
        client = _client(TokenValidationMiddleware, [descope_config("P123"), ory_config(ORY_ISSUER, "api")])

        assert _get(client, _jwt(descope_claims)).status_code == 200

        config = validate.await_args.kwargs["token_validation_config"]
        assert config.require_https is True
        assert config.algorithms == ["RS256"]

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_a_provider_that_pins_nothing_leaves_the_set_to_the_library(self, validate):
        validate.return_value = _claims()

        _get(_client(TokenValidationMiddleware, [_keycloak(algorithms=frozenset())]), _jwt(_claims()))

        assert validate.await_args.kwargs["token_validation_config"].algorithms is None

    @pytest.mark.parametrize("aud", [None, "account", APP_CLIENT, ["account"]])
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_missing_or_foreign_audience_is_refused(self, validate, aud):
        validate.return_value = _claims(aud=aud)

        response = _get(_client(TokenValidationMiddleware, self._providers()), _jwt(_claims(aud=aud)))

        assert response.status_code == 401
        assert response.json() == UNIFORM_401

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_descope_issuer_is_refused_by_a_keycloak_only_backend_before_any_crypto(self, validate):
        descope_claims = {"iss": "https://api.descope.com/P123", "sub": "descope-user", "aud": MEMBER}
        validate.return_value = descope_claims
        client = _client(TokenValidationMiddleware, [descope_config(""), _keycloak()])

        response = _get(client, _jwt(descope_claims))

        assert response.status_code == 401
        assert response.json() == UNIFORM_401
        validate.assert_not_awaited()

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_keycloak_issuer_is_refused_by_a_descope_only_backend_before_any_crypto(self, validate):
        validate.return_value = _claims()
        client = _client(TokenValidationMiddleware, [descope_config("P123")])

        response = _get(client, _jwt(_claims()))

        assert response.status_code == 401
        assert response.json() == UNIFORM_401
        validate.assert_not_awaited()

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_a_token_validated_as_keycloak_but_claiming_another_issuer_is_refused(self, validate):
        """The unverified ``iss`` only picks the provider; the verified one must still be accepted."""
        validate.return_value = _claims(iss="https://api.descope.com/P123")

        response = _get(_client(TokenValidationMiddleware, self._providers()), _jwt(_claims()))

        assert response.status_code == 401
        assert response.json() == UNIFORM_401

    def test_legacy_arguments_still_build_the_same_providers(self):
        """The pre-builder keyword arguments stay for callers that construct the middleware by hand."""
        middleware = TokenValidationMiddleware(
            FastAPI(), descope_project_id="P123", ory_issuer_url=ORY_ISSUER, ory_audience="api"
        )

        assert middleware._providers == build_provider_configs(
            descope_project_id="P123", ory_issuer_url=ORY_ISSUER, ory_audience="api"
        )


class TestGatewayClaimsMiddleware:
    def _client(self) -> TestClient:
        return _client(GatewayClaimsMiddleware, [descope_config("P123"), _keycloak()])

    def test_valid_token_is_attributed_to_keycloak(self):
        response = _get(self._client(), _jwt(_claims()))

        assert response.status_code == 200
        assert response.json() == {"sub": "kc-user", "provider": "Keycloak"}

    @pytest.mark.parametrize("alg", ["ES256", "none"])
    def test_non_rs256_header_is_refused(self, alg):
        response = _get(self._client(), _jwt(_claims(), header={"alg": alg}))

        assert response.status_code == 401
        assert response.json() == UNIFORM_401

    @pytest.mark.parametrize("aud", [None, "account"])
    def test_missing_or_foreign_audience_is_refused(self, aud):
        response = _get(self._client(), _jwt(_claims(aud=aud)))

        assert response.status_code == 401
        assert response.json() == UNIFORM_401


class TestFactory:
    """The factory hands both middlewares the shared builder's list."""

    def _configure(self, monkeypatch, mode: str) -> dict:
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
        return {"name": token_middleware.cls.__name__, "kwargs": token_middleware.kwargs}

    @pytest.mark.parametrize(
        ("mode", "middleware"),
        [("standalone", TokenValidationMiddleware.__name__), ("gateway", GatewayClaimsMiddleware.__name__)],
    )
    def test_keycloak_is_registered_from_the_environment_alone(self, clean_env, mode, middleware):
        clean_env.setenv("DESCOPE_PROJECT_ID", "P123")
        clean_env.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
        clean_env.setenv("KEYCLOAK_AUDIENCES", "member,tenant-admin")
        set_model_environment(clean_env, MEMBER, TENANT_ADMIN)

        registered = self._configure(clean_env, mode)

        assert registered["name"] == middleware
        assert registered["kwargs"]["providers"] == configured_providers()
        assert [provider.name for provider in registered["kwargs"]["providers"]] == ["Descope", "Keycloak"]

    def test_startup_fails_when_the_issuer_is_set_and_no_audience_is(self, clean_env):
        clean_env.setenv("KEYCLOAK_ISSUER_URL", ISSUER)

        with pytest.raises(ProviderConfigurationError, match="KEYCLOAK_AUDIENCES"):
            self._configure(clean_env, "standalone")
