"""A provider is one registration plus a claims extractor.

The second RP-initiated provider in this file is registered by the test alone.
Nothing in the factory, the two middlewares, the logout router or the logout
service names it, so these tests fail if any of them stops taking its providers
from ``configured_providers``.
"""

import ast
import base64
import importlib
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from py_identity_model import DiscoveryPolicy
from slowapi.errors import RateLimitExceeded

from app.dependencies import rbac
from app.middleware import providers as provider_registry
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import (
    KeycloakTenantClaims,
    ProviderConfigurationError,
    ProviderTokenConfig,
    TenantClaims,
    build_provider_configs,
    configured_providers,
    descope_config,
    descope_tenant_claims,
    keycloak_config,
    ory_config,
    refuse_canonical_descope_managed,
)
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.routers import auth as auth_router
from tests.helpers.keycloak_model import MODEL_VARIABLES, builder_settings, model_settings, set_model_environment

DESCOPE_PROJECT = "P123"
ORY_ISSUER = "https://ory.example.test"
ORY_AUDIENCE = "https://identity-stack-api"
ORY_CLIENT_ID = "ory-spa-client"
ORY_REDIRECT = "https://app.example.test/ory-signed-out"
ORY_END_SESSION = f"{ORY_ISSUER}/oauth2/sessions/logout"

SECOND_NAME = "Second"
SECOND_ISSUER = "http://second.example.test:9000"
SECOND_AUDIENCE = "second-api"
SECOND_CLIENT_ID = "second-spa-client"
SECOND_REDIRECT = "https://app.example.test/second-signed-out"
SECOND_END_SESSION = f"{SECOND_ISSUER}/logout"

ID_TOKEN = "the-id-token"
PROBE = "/api/probe"

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


def _second_provider() -> ProviderTokenConfig:
    """A second RP-initiated provider, on plain HTTP, with its own logout settings."""
    return ProviderTokenConfig(
        name=SECOND_NAME,
        accepted_issuers=frozenset({SECOND_ISSUER}),
        disco_address=f"{SECOND_ISSUER}/.well-known/openid-configuration",
        audience=SECOND_AUDIENCE,
        require_audience=True,
        require_https=False,
        logout_kind="rp_initiated",
        logout_client_id=SECOND_CLIENT_ID,
        post_logout_redirect_uri=SECOND_REDIRECT,
    )


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _jwt(claims: dict) -> str:
    """An unsigned JWT. Standalone tests patch ``validate_token``; the gateway reads it as is."""
    return f"{_segment({'alg': 'RS256'})}.{_segment(claims)}.sig"


def _second_claims(**overrides) -> dict:
    return {
        "iss": SECOND_ISSUER,
        "sub": "second-user",
        "aud": SECOND_AUDIENCE,
        "exp": int(time.time()) + 300,
        **overrides,
    }


def _ory_claims(**overrides) -> dict:
    return {"iss": ORY_ISSUER, "sub": "ory-user", "aud": ORY_AUDIENCE, "exp": int(time.time()) + 300, **overrides}


def _descope_claims(**overrides) -> dict:
    return {
        "iss": f"https://api.descope.com/{DESCOPE_PROJECT}",
        "sub": "descope-user",
        "exp": int(time.time()) + 300,
        **overrides,
    }


@pytest.fixture
def clean_env(monkeypatch):
    limiter.reset()
    for name in _PROVIDER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DESCOPE_PROJECT_ID", DESCOPE_PROJECT)
    return monkeypatch


@pytest.fixture
def with_ory(clean_env):
    clean_env.setenv("ORY_ISSUER_URL", ORY_ISSUER)
    clean_env.setenv("ORY_AUDIENCE", ORY_AUDIENCE)
    clean_env.setenv("ORY_CLIENT_ID", ORY_CLIENT_ID)
    clean_env.setenv("ORY_POST_LOGOUT_REDIRECT_URI", ORY_REDIRECT)
    return clean_env


@pytest.fixture
def second_registered(with_ory):
    """Register the second provider: one entry appended, and no other edit."""
    with_ory.setattr(
        provider_registry,
        "PROVIDER_REGISTRATIONS",
        [*provider_registry.PROVIDER_REGISTRATIONS, _second_provider],
    )
    return with_ory


def _configured_app(monkeypatch, mode: str) -> FastAPI:
    """An app built by the shipped factory in ``mode``, with the logout router and a probe."""
    monkeypatch.setenv("DEPLOYMENT_MODE", mode)
    import app.middleware.factory as factory

    factory = importlib.reload(factory)
    try:
        app = FastAPI()
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
        app.include_router(auth_router.router, prefix="/api")

        @app.get(PROBE)
        async def probe(request: Request):
            return {
                "provider": request.state.principal.identity.authentication_type,
                "registered_as": request.state.provider.name,
                "tenant_id": request.state.tenant_id,
            }

        factory.configure_middleware(app)
    finally:
        monkeypatch.delenv("DEPLOYMENT_MODE")
        importlib.reload(factory)
    return app


def _token_middleware(app: FastAPI):
    names = {TokenValidationMiddleware.__name__, GatewayClaimsMiddleware.__name__}
    (middleware,) = [m for m in app.user_middleware if m.cls.__name__ in names]
    return middleware


def _bearer(claims: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {_jwt(claims)}"}


class TestRegistry:
    def test_the_shipped_registrations_build_descope_ory_and_keycloak(self, with_ory):
        with_ory.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.example.test/realms/identity-stack")
        with_ory.setenv("KEYCLOAK_AUDIENCES", "member")
        set_model_environment(with_ory, "member")

        assert [provider.name for provider in configured_providers()] == ["Descope", "Ory", "Keycloak"]

    def test_a_registration_that_returns_nothing_adds_no_provider(self, clean_env):
        assert [provider.name for provider in configured_providers()] == ["Descope"]

    def test_an_appended_registration_is_in_the_list(self, second_registered):
        by_name = {provider.name: provider for provider in configured_providers()}

        assert sorted(by_name) == ["Descope", "Ory", SECOND_NAME]
        assert by_name[SECOND_NAME] == _second_provider()

    def test_two_providers_with_one_name_are_refused(self, second_registered):
        """Dependencies and logout pick a provider by name, so a shared name is two providers in one."""
        twin = replace(_second_provider(), name=SECOND_NAME.upper(), accepted_issuers=frozenset({"https://twin.test"}))
        second_registered.setattr(
            provider_registry,
            "PROVIDER_REGISTRATIONS",
            [*provider_registry.PROVIDER_REGISTRATIONS, lambda: twin],
        )

        with pytest.raises(ProviderConfigurationError, match="more than once"):
            configured_providers()

    def test_two_providers_sharing_an_issuer_are_refused(self, second_registered):
        """The middlewares pick a provider by issuer: a shared one gives a token the wrong provider's rules."""
        squatter = replace(
            _second_provider(), name="Squatter", accepted_issuers=frozenset({ORY_ISSUER, "https://x.test"})
        )
        second_registered.setattr(
            provider_registry,
            "PROVIDER_REGISTRATIONS",
            [*provider_registry.PROVIDER_REGISTRATIONS, lambda: squatter],
        )

        with pytest.raises(ProviderConfigurationError, match=ORY_ISSUER):
            configured_providers()

    def test_a_duplicate_stops_startup(self, second_registered):
        second_registered.setattr(
            provider_registry,
            "PROVIDER_REGISTRATIONS",
            [*provider_registry.PROVIDER_REGISTRATIONS, _second_provider],
        )

        with pytest.raises(ProviderConfigurationError):
            _configured_app(second_registered, "standalone")


class TestClaimsExtractors:
    def test_descope_declares_the_dct_and_tenants_extractor(self):
        assert descope_config(DESCOPE_PROJECT).tenant_claims is descope_tenant_claims

    def test_ory_declares_none(self):
        assert ory_config(ORY_ISSUER, ORY_AUDIENCE).tenant_claims is None

    def test_keycloak_declares_its_own(self):
        """The organization and the client roles of its token, never the canonical model."""
        keycloak = keycloak_config("https://kc.test/realms/r", frozenset({"member"}), **model_settings("member"))

        assert isinstance(keycloak.tenant_claims, KeycloakTenantClaims)
        assert keycloak.token_authorized_only is True

    def test_descope_extractor_reads_the_current_tenant_and_its_grants(self):
        claims = {
            "dct": "t1",
            "tenants": {
                "t1": {"roles": ["admin"], "permissions": ["projects.read"]},
                "t2": {"roles": ["owner"], "permissions": ["billing.manage"]},
            },
        }

        assert descope_tenant_claims(claims) == TenantClaims(
            tenant_id="t1", roles=["admin"], permissions=["projects.read"]
        )

    @pytest.mark.parametrize("claims", [{}, {"dct": ""}, {"tenants": {"t1": {"roles": ["admin"]}}}])
    def test_descope_extractor_names_no_tenant_without_a_dct(self, claims):
        assert descope_tenant_claims(claims) == TenantClaims(tenant_id=None, roles=[], permissions=[])

    @pytest.mark.parametrize("dct", [["t1"], {"t1": True}, 7])
    def test_descope_extractor_refuses_a_dct_that_is_not_a_string(self, dct):
        claims = {"dct": dct, "tenants": {"t1": {"roles": ["admin"]}}}

        assert descope_tenant_claims(claims) == TenantClaims(tenant_id=None, roles=[], permissions=[])

    @pytest.mark.parametrize(
        "tenants",
        [None, ["t1"], "t1", {"t1": None}, {"t1": ["admin"]}, {"t1": {"roles": "admin", "permissions": None}}],
    )
    def test_descope_extractor_grants_nothing_from_malformed_tenants(self, tenants):
        assert descope_tenant_claims({"dct": "t1", "tenants": tenants}) == TenantClaims(
            tenant_id="t1", roles=[], permissions=[]
        )

    def test_descope_extractor_keeps_the_strings_of_a_mixed_grant_list(self):
        """An entry that is not a string is dropped and the strings beside it
        stay granted, as ``"admin" in ["admin", 7]`` always answered."""
        tenants = {"t1": {"roles": ["admin", 7, None], "permissions": [{"p": 1}, "projects.read"]}}

        assert descope_tenant_claims({"dct": "t1", "tenants": tenants}) == TenantClaims(
            tenant_id="t1", roles=["admin"], permissions=["projects.read"]
        )

    def test_descope_extractor_grants_nothing_without_a_tenants_claim(self):
        assert descope_tenant_claims({"dct": "t1"}) == TenantClaims(tenant_id="t1", roles=[], permissions=[])


class TestRegisteredProviderReachesTheMiddlewares:
    @pytest.mark.parametrize(
        ("mode", "middleware"),
        [("standalone", TokenValidationMiddleware.__name__), ("gateway", GatewayClaimsMiddleware.__name__)],
    )
    def test_the_factory_hands_it_to_the_token_middleware(self, second_registered, mode, middleware):
        registered = _token_middleware(_configured_app(second_registered, mode))

        assert registered.cls.__name__ == middleware
        assert registered.kwargs["providers"] == configured_providers()
        assert _second_provider() in registered.kwargs["providers"]

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_standalone_validates_its_tokens_with_its_own_https_switch(self, validate, second_registered):
        client = TestClient(_configured_app(second_registered, "standalone"))

        validate.return_value = _second_claims()
        second = client.get(PROBE, headers=_bearer(_second_claims()))
        second_call = validate.await_args.kwargs
        validate.return_value = _ory_claims()
        ory = client.get(PROBE, headers=_bearer(_ory_claims()))
        ory_call = validate.await_args.kwargs
        validate.return_value = _descope_claims()
        descope = client.get(PROBE, headers=_bearer(_descope_claims()))
        descope_call = validate.await_args.kwargs

        assert second.json() == {"provider": SECOND_NAME, "registered_as": SECOND_NAME, "tenant_id": None}
        assert second_call["disco_doc_address"] == f"{SECOND_ISSUER}/.well-known/openid-configuration"
        assert second_call["token_validation_config"].require_https is False
        assert ory.json() == {"provider": "Ory", "registered_as": "Ory", "tenant_id": None}
        assert ory_call["token_validation_config"].require_https is True
        assert descope.json() == {"provider": "Descope", "registered_as": "Descope", "tenant_id": None}
        assert descope_call["token_validation_config"].require_https is True

    def test_gateway_attributes_its_tokens_to_it(self, second_registered):
        client = TestClient(_configured_app(second_registered, "gateway"))

        response = client.get(PROBE, headers=_bearer(_second_claims()))

        assert response.status_code == 200
        assert response.json() == {"provider": SECOND_NAME, "registered_as": SECOND_NAME, "tenant_id": None}

    def test_gateway_still_binds_its_tokens_to_its_audience(self, second_registered):
        client = TestClient(_configured_app(second_registered, "gateway"))

        response = client.get(PROBE, headers=_bearer(_second_claims(aud=ORY_AUDIENCE)))

        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid or expired token"}


class TestRegisteredProviderLogsOut:
    def _logout(self, monkeypatch, claims: dict, end_session_endpoint: str):
        with (
            patch("app.middleware.auth.validate_token", new_callable=AsyncMock) as validate,
            patch("app.services.logout.get_discovery_document", new_callable=AsyncMock) as disco,
        ):
            validate.return_value = claims
            disco.return_value = SimpleNamespace(is_successful=True, end_session_endpoint=end_session_endpoint)
            client = TestClient(_configured_app(monkeypatch, "standalone"))
            response = client.post("/api/auth/logout", headers=_bearer(claims), json={"id_token": ID_TOKEN})
        return response, disco.await_args.args[0]

    def test_each_rp_initiated_provider_uses_its_own_client_id_and_redirect(self, second_registered):
        second, _ = self._logout(second_registered, _second_claims(), SECOND_END_SESSION)
        ory, _ = self._logout(second_registered, _ory_claims(), ORY_END_SESSION)

        assert second.status_code == 200
        assert parse_qs(urlparse(second.json()["logout_url"]).query) == {
            "id_token_hint": [ID_TOKEN],
            "client_id": [SECOND_CLIENT_ID],
            "post_logout_redirect_uri": [SECOND_REDIRECT],
        }
        assert second.json()["logout_url"].startswith(f"{SECOND_END_SESSION}?")
        assert ory.status_code == 200
        assert parse_qs(urlparse(ory.json()["logout_url"]).query) == {
            "id_token_hint": [ID_TOKEN],
            "client_id": [ORY_CLIENT_ID],
            "post_logout_redirect_uri": [ORY_REDIRECT],
        }
        assert ory.json()["logout_url"].startswith(f"{ORY_END_SESSION}?")

    def test_plain_http_discovery_is_allowed_for_it_and_refused_for_the_other(self, second_registered):
        _, second_request = self._logout(second_registered, _second_claims(), SECOND_END_SESSION)
        _, ory_request = self._logout(second_registered, _ory_claims(), ORY_END_SESSION)

        assert second_request.address == f"{SECOND_ISSUER}/.well-known/openid-configuration"
        assert second_request.policy == DiscoveryPolicy(require_https=False)
        assert ory_request.policy == DiscoveryPolicy(require_https=True)

    def test_plain_http_is_refused_for_every_provider_that_did_not_ask_for_it(self, second_registered):
        """One provider's switch moves no other's: each one's own setting is what validation is handed."""
        requires_https = {provider.name: provider.require_https for provider in configured_providers()}

        assert requires_https == {"Descope": True, "Ory": True, SECOND_NAME: False}


class TestTenantIdComesFromTheExtractor:
    """``request.state.tenant_id`` is what the provider's extractor says, not the ``dct`` claim."""

    FOREIGN = {"dct": "t1", "tenants": {"t1": {"roles": ["admin"]}}}

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_standalone_reads_descopes_dct_through_its_extractor(self, validate, with_ory):
        validate.return_value = _descope_claims(**self.FOREIGN)
        client = TestClient(_configured_app(with_ory, "standalone"))

        response = client.get(PROBE, headers=_bearer(_descope_claims()))

        assert response.json() == {"provider": "Descope", "registered_as": "Descope", "tenant_id": "t1"}

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_standalone_does_not_read_a_dct_on_a_provider_without_an_extractor(self, validate, with_ory):
        validate.return_value = _ory_claims(**self.FOREIGN)
        client = TestClient(_configured_app(with_ory, "standalone"))

        response = client.get(PROBE, headers=_bearer(_ory_claims()))

        assert response.json() == {"provider": "Ory", "registered_as": "Ory", "tenant_id": None}

    def test_gateway_reads_descopes_dct_through_its_extractor(self, with_ory):
        client = TestClient(_configured_app(with_ory, "gateway"))

        response = client.get(PROBE, headers=_bearer(_descope_claims(**self.FOREIGN)))

        assert response.json() == {"provider": "Descope", "registered_as": "Descope", "tenant_id": "t1"}

    def test_gateway_does_not_read_a_dct_on_a_provider_without_an_extractor(self, with_ory):
        client = TestClient(_configured_app(with_ory, "gateway"))

        response = client.get(PROBE, headers=_bearer(_ory_claims(**self.FOREIGN)))

        assert response.json() == {"provider": "Ory", "registered_as": "Ory", "tenant_id": None}


def _raising_tenant_claims(claims: dict) -> TenantClaims:
    raise KeyError("org")


def _malformed_tenant_claims(claims: dict) -> TenantClaims:
    return TenantClaims(tenant_id=claims.get("org"), roles=[], permissions=[])


def _register_second_with(monkeypatch, extractor) -> None:
    provider = replace(_second_provider(), tenant_claims=extractor)
    monkeypatch.setattr(
        provider_registry,
        "PROVIDER_REGISTRATIONS",
        [*provider_registry.PROVIDER_REGISTRATIONS, lambda: provider],
    )


class TestAuthenticationFailsTheSameWayInBothModes:
    """Whatever goes wrong while a token is turned into a principal is the uniform 401."""

    UNIFORM_401 = {"detail": "Invalid or expired token"}

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_standalone_answers_401_when_the_extractor_raises(self, validate, with_ory):
        _register_second_with(with_ory, _raising_tenant_claims)
        validate.return_value = _second_claims()
        client = TestClient(_configured_app(with_ory, "standalone"))

        response = client.get(PROBE, headers=_bearer(_second_claims()))

        assert response.status_code == 401
        assert response.json() == self.UNIFORM_401

    def test_gateway_answers_401_when_the_extractor_raises(self, with_ory):
        _register_second_with(with_ory, _raising_tenant_claims)
        client = TestClient(_configured_app(with_ory, "gateway"))

        response = client.get(PROBE, headers=_bearer(_second_claims()))

        assert response.status_code == 401
        assert response.json() == self.UNIFORM_401

    @patch("app.middleware.auth.to_principal", side_effect=ValueError("claims cannot make a principal"))
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_standalone_answers_401_when_the_principal_cannot_be_built(self, validate, _to_principal, with_ory):
        validate.return_value = _ory_claims()
        client = TestClient(_configured_app(with_ory, "standalone"))

        response = client.get(PROBE, headers=_bearer(_ory_claims()))

        assert response.status_code == 401
        assert response.json() == self.UNIFORM_401

    @patch("app.middleware.claims.to_principal", side_effect=ValueError("claims cannot make a principal"))
    def test_gateway_answers_401_when_the_principal_cannot_be_built(self, _to_principal, with_ory):
        client = TestClient(_configured_app(with_ory, "gateway"))

        response = client.get(PROBE, headers=_bearer(_ory_claims()))

        assert response.status_code == 401
        assert response.json() == self.UNIFORM_401

    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    def test_a_downstream_failure_is_still_not_a_401(self, validate, with_ory):
        """Only authentication is answered 401: the handler's own error keeps its status."""
        validate.return_value = _ory_claims()
        app = _configured_app(with_ory, "standalone")

        @app.get("/api/broken")
        async def broken():
            raise RuntimeError("the handler failed")

        response = TestClient(app, raise_server_exceptions=False).get("/api/broken", headers=_bearer(_ory_claims()))

        assert response.status_code == 500

    @pytest.mark.parametrize("tenant", [7, ["org-1"], {"id": "org-1"}, ""])
    def test_a_tenant_that_is_not_a_non_empty_string_is_no_tenant(self, with_ory, tenant):
        _register_second_with(with_ory, _malformed_tenant_claims)
        client = TestClient(_configured_app(with_ory, "gateway"))

        response = client.get(PROBE, headers=_bearer(_second_claims(org=tenant)))

        assert response.json() == {"provider": SECOND_NAME, "registered_as": SECOND_NAME, "tenant_id": None}


class TestOneAssemblyBehindBothBuilders:
    """``build_provider_configs`` is the same walk as ``configured_providers``, over explicit values."""

    # Plain HTTP is switched on below, which startup allows only for a local issuer.
    KEYCLOAK_ISSUER = "http://keycloak.localhost:8180/realms/identity-stack"

    def test_it_builds_what_the_registrations_build_from_the_same_values(self, with_ory):
        with_ory.setenv("KEYCLOAK_ISSUER_URL", self.KEYCLOAK_ISSUER)
        with_ory.setenv("KEYCLOAK_AUDIENCES", "member")
        set_model_environment(with_ory, "member")
        with_ory.setenv("KEYCLOAK_REQUIRE_HTTPS", "false")
        with_ory.setenv("KEYCLOAK_POST_LOGOUT_REDIRECT_URI", "https://app.example.test/kc-signed-out")

        by_hand = build_provider_configs(
            descope_project_id=DESCOPE_PROJECT,
            ory_issuer_url=ORY_ISSUER,
            ory_audience=ORY_AUDIENCE,
            ory_client_id=ORY_CLIENT_ID,
            ory_post_logout_redirect_uri=ORY_REDIRECT,
            keycloak_issuer_url=self.KEYCLOAK_ISSUER,
            keycloak_audiences=frozenset({"member"}),
            keycloak_require_https=False,
            keycloak_post_logout_redirect_uri="https://app.example.test/kc-signed-out",
            **builder_settings("member"),
        )

        assert by_hand == configured_providers()

    def test_a_shared_issuer_is_refused_by_hand_too(self):
        # A Keycloak issuer is <origin>/realms/<realm> (its Admin API is derived from it), so the
        # issuer both providers claim has that form.
        shared = "https://shared.example.test/realms/identity-stack"
        with pytest.raises(ProviderConfigurationError, match=shared):
            build_provider_configs(
                descope_project_id=DESCOPE_PROJECT,
                ory_issuer_url=shared,
                ory_audience=ORY_AUDIENCE,
                keycloak_issuer_url=shared,
                keycloak_audiences=frozenset({"member"}),
                **builder_settings("member"),
            )

    @pytest.mark.parametrize("spelling", [f"{ORY_ISSUER}/", ORY_ISSUER.upper(), f"{ORY_ISSUER.upper()}//"])
    def test_another_spelling_of_a_taken_issuer_is_refused(self, second_registered, spelling):
        squatter = replace(_second_provider(), name="Squatter", accepted_issuers=frozenset({spelling}))
        second_registered.setattr(
            provider_registry,
            "PROVIDER_REGISTRATIONS",
            [*provider_registry.PROVIDER_REGISTRATIONS, lambda: squatter],
        )

        with pytest.raises(ProviderConfigurationError, match="another provider already accepts"):
            configured_providers()

    def test_issuers_that_differ_in_the_case_of_the_path_are_two_issuers(self, second_registered):
        """A path is case-sensitive: two realms spelled ``Acme`` and ``acme`` are two realms."""
        upper = replace(
            _second_provider(), name="RealmUpper", accepted_issuers=frozenset({"https://kc.test/realms/Acme"})
        )
        lower = replace(
            _second_provider(), name="RealmLower", accepted_issuers=frozenset({"https://kc.test/realms/acme"})
        )
        second_registered.setattr(
            provider_registry,
            "PROVIDER_REGISTRATIONS",
            [*provider_registry.PROVIDER_REGISTRATIONS, lambda: upper, lambda: lower],
        )

        names = {provider.name for provider in configured_providers()}

        assert {"RealmUpper", "RealmLower"} <= names

    def test_issuers_that_differ_in_the_case_of_the_host_are_one_issuer(self, second_registered):
        first = replace(
            _second_provider(), name="HostLower", accepted_issuers=frozenset({"https://kc.test/realms/acme"})
        )
        again = replace(
            _second_provider(), name="HostUpper", accepted_issuers=frozenset({"HTTPS://KC.TEST/realms/acme/"})
        )
        second_registered.setattr(
            provider_registry,
            "PROVIDER_REGISTRATIONS",
            [*provider_registry.PROVIDER_REGISTRATIONS, lambda: first, lambda: again],
        )

        with pytest.raises(ProviderConfigurationError, match="another provider already accepts"):
            configured_providers()


class TestADescopeManagedProviderIsNeverCanonical:
    """Descope's ids must not be swapped for canonical ones on the routes that hand them to Descope."""

    def test_listing_descope_as_canonical_is_refused(self):
        providers = [descope_config(DESCOPE_PROJECT), ory_config(ORY_ISSUER, ORY_AUDIENCE)]

        with pytest.raises(ProviderConfigurationError, match="CANONICAL_RBAC_PROVIDERS") as refused:
            refuse_canonical_descope_managed(providers, {"ory", "descope"})

        assert "'Descope'" in str(refused.value)

    def test_the_default_set_is_accepted(self):
        providers = [descope_config(DESCOPE_PROJECT), ory_config(ORY_ISSUER, ORY_AUDIENCE)]

        assert refuse_canonical_descope_managed(providers, {"ory"}) is None

    def test_a_canonical_name_no_provider_carries_is_accepted(self):
        assert refuse_canonical_descope_managed([ory_config(ORY_ISSUER, ORY_AUDIENCE)], {"ory", "descope"}) is None

    @pytest.mark.parametrize("mode", ["standalone", "gateway"])
    def test_it_stops_startup(self, with_ory, mode):
        with_ory.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", {"ory", "descope"})

        with pytest.raises(ProviderConfigurationError, match="CANONICAL_RBAC_PROVIDERS"):
            _configured_app(with_ory, mode)

    @pytest.mark.parametrize("mode", ["standalone", "gateway"])
    def test_the_default_set_starts(self, with_ory, mode):
        assert _token_middleware(_configured_app(with_ory, mode)) is not None


class TestAnUnreadableTokenIsOneLogLine:
    """Anyone can send a garbage bearer, so it must not cost a traceback each time."""

    UNIFORM_401 = {"detail": "Invalid or expired token"}
    GATEWAY_LOGGER = "app.middleware.claims"

    @pytest.mark.parametrize(
        "token",
        ["a.b.c", "header.!!!!.sig", f"header.{base64.urlsafe_b64encode(b'not json').decode()}.sig", "h.\u00e9.s"],
        ids=["short segments", "not base64", "not json", "not ascii"],
    )
    def test_gateway_logs_a_payload_it_cannot_decode_without_a_traceback(self, with_ory, caplog, token):
        client = TestClient(_configured_app(with_ory, "gateway"))

        with caplog.at_level("WARNING", logger=self.GATEWAY_LOGGER):
            response = client.get(PROBE, headers={"Authorization": f"Bearer {token}".encode()})

        assert response.status_code == 401
        assert response.json() == self.UNIFORM_401
        (record,) = [r for r in caplog.records if r.name == self.GATEWAY_LOGGER]
        assert record.getMessage() == "GatewayClaims rejected: the token payload could not be decoded"
        assert record.exc_info is None

    def test_gateway_keeps_the_traceback_for_an_extractor_that_raises(self, with_ory, caplog):
        _register_second_with(with_ory, _raising_tenant_claims)
        client = TestClient(_configured_app(with_ory, "gateway"))

        with caplog.at_level("WARNING", logger=self.GATEWAY_LOGGER):
            response = client.get(PROBE, headers=_bearer(_second_claims()))

        assert response.status_code == 401
        (record,) = [r for r in caplog.records if r.name == self.GATEWAY_LOGGER]
        assert record.exc_info is not None
        assert record.exc_info[0] is KeyError


class TestOnlyDescopeIsDescopeManaged:
    def test_descope_says_its_ids_are_descopes(self):
        assert descope_config(DESCOPE_PROJECT).descope_managed is True

    def test_no_other_provider_does(self):
        others = [
            ory_config(ORY_ISSUER, ORY_AUDIENCE),
            keycloak_config(
                "https://keycloak.example.test/realms/r", frozenset({"member"}), **model_settings("member")
            ),
            _second_provider(),
        ]

        assert [provider.descope_managed for provider in others] == [False, False, False]


def _state_attribute_writes(func: ast.AST) -> set[str]:
    """Names of the ``<something>.state.<name>`` attributes ``func`` assigns."""
    return {
        target.attr
        for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "state"
    }


def _functions_setting_claims_without_a_provider(source: str) -> list[str]:
    return [
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "claims" in (written := _state_attribute_writes(node))
        and "provider" not in written
    ]


class TestClaimsAreNeverRecordedWithoutTheirProvider:
    """The resolver reads a request with no recorded provider as Descope's (conventions §18).

    Only a hand-built request may be like that, so every place in ``app/`` that
    sets ``request.state.claims`` must set ``request.state.provider`` beside it.
    """

    APP = Path(__file__).resolve().parents[2] / "app"

    def test_every_writer_of_the_claims_records_the_provider(self):
        offenders = {
            str(path.relative_to(self.APP)): names
            for path in sorted(self.APP.rglob("*.py"))
            if (names := _functions_setting_claims_without_a_provider(path.read_text()))
        }

        assert offenders == {}

    def test_both_middlewares_are_seen_as_writers(self):
        writers = sorted(
            str(path.relative_to(self.APP))
            for path in self.APP.rglob("*.py")
            if "claims" in _state_attribute_writes(ast.parse(path.read_text()))
        )

        assert writers == ["middleware/auth.py", "middleware/claims.py"]

    def test_the_scan_flags_a_writer_that_records_no_provider(self):
        source = "def dispatch(request, claims):\n    request.state.claims = claims\n"

        assert _functions_setting_claims_without_a_provider(source) == ["dispatch"]
