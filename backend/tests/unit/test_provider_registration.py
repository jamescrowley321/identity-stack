"""A provider is one registration plus a claims extractor.

The second RP-initiated provider in this file is registered by the test alone.
Nothing in the factory, the two middlewares, the logout router or the logout
service names it, so these tests fail if any of them stops taking its providers
from ``configured_providers``.
"""

import base64
import importlib
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from py_identity_model import DiscoveryPolicy
from slowapi.errors import RateLimitExceeded

from app.middleware import providers as provider_registry
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import (
    ProviderConfigurationError,
    ProviderTokenConfig,
    TenantClaims,
    configured_providers,
    descope_config,
    descope_tenant_claims,
    keycloak_config,
    ory_config,
)
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.routers import auth as auth_router

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

    def test_ory_and_keycloak_declare_none(self):
        assert ory_config(ORY_ISSUER, ORY_AUDIENCE).tenant_claims is None
        assert keycloak_config("https://kc.test/realms/r", frozenset({"member"})).tenant_claims is None

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

        assert second.json() == {"provider": SECOND_NAME, "registered_as": SECOND_NAME, "tenant_id": None}
        assert second_call["disco_doc_address"] == f"{SECOND_ISSUER}/.well-known/openid-configuration"
        assert second_call["token_validation_config"].require_https is False
        assert ory.json() == {"provider": "Ory", "registered_as": "Ory", "tenant_id": None}
        assert ory_call["token_validation_config"].require_https is True

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
