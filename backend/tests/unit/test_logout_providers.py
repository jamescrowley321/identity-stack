"""RP-initiated logout takes its client id and redirect URI from the provider.

The Ory cases pin the whole response body, so a change to where those settings
come from cannot change a byte of what Ory's SPA receives.
"""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from py_identity_model import DiscoveryPolicy
from slowapi.errors import RateLimitExceeded

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import configured_providers
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.routers import auth as auth_router
from app.services.logout import _logout_client_id
from tests.helpers.keycloak_model import API_AUDIENCE, KEYCLOAK_VARIABLES, SPA_CLIENT_ID, set_client_environment

ORY_ISSUER = "https://ory.example.test"
ORY_AUDIENCE = "https://identity-stack-api"
ORY_END_SESSION = f"{ORY_ISSUER}/oauth2/sessions/logout"
ORY_CLIENT_ID = "ory-spa-client"
ORY_REDIRECT = "https://app.example.test/signed-out"

KEYCLOAK_ISSUER = "https://keycloak.example.test/realms/identity-stack"
KEYCLOAK_END_SESSION = f"{KEYCLOAK_ISSUER}/protocol/openid-connect/logout"
KEYCLOAK_REDIRECT = "https://app.example.test/login"

ZITADEL_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "zitadel"
ZITADEL_DISCOVERY = json.loads((ZITADEL_FIXTURES / "discovery.json").read_text())
ZITADEL_ISSUER = ZITADEL_DISCOVERY["issuer"]
ZITADEL_END_SESSION = ZITADEL_DISCOVERY["end_session_endpoint"]
ZITADEL_PROJECT_ID = "399781183140937731"
ZITADEL_SPA_CLIENT_ID = "399781187016540163"
ZITADEL_REDIRECT = "http://localhost:3000/signed-out"

ID_TOKEN = "the-id-token"


def _jwt(payload: dict) -> str:
    """An unsigned JWT: ``validate_token`` is patched, only the header and ``iss`` are read."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def _app() -> FastAPI:
    """The auth router behind the middleware, both on the environment's provider list."""
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(auth_router.router, prefix="/api")
    app.add_middleware(TokenValidationMiddleware, providers=configured_providers())
    return app


async def _logout(claims: dict, end_session_endpoint: str, *, id_token: str | None = ID_TOKEN):
    """POST /api/auth/logout as a principal with ``claims``; returns (response, discovery mock)."""
    body = {"id_token": id_token} if id_token is not None else None
    with (
        patch("app.middleware.auth.validate_token", new_callable=AsyncMock) as validate,
        patch("app.services.logout.get_discovery_document", new_callable=AsyncMock) as disco,
    ):
        validate.return_value = claims
        disco.return_value = SimpleNamespace(is_successful=True, end_session_endpoint=end_session_endpoint)
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
            response = await client.post(
                "/api/auth/logout",
                headers={"Authorization": f"Bearer {_jwt(claims)}"},
                json=body,
            )
    return response, disco


def _query(logout_url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(logout_url).query)


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    limiter.reset()
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    for name in (
        "ORY_ISSUER_URL",
        "ORY_AUDIENCE",
        "ORY_REQUIRE_AUDIENCE",
        "ORY_CLIENT_ID",
        "ORY_POST_LOGOUT_REDIRECT_URI",
        *KEYCLOAK_VARIABLES,
        "ZITADEL_ISSUER_URL",
        "ZITADEL_AUDIENCE",
        "ZITADEL_CLIENT_ID",
        "ZITADEL_REQUIRE_HTTPS",
        "ZITADEL_POST_LOGOUT_REDIRECT_URI",
        "FRONTEND_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def ory(monkeypatch):
    monkeypatch.setenv("ORY_ISSUER_URL", ORY_ISSUER)
    monkeypatch.setenv("ORY_AUDIENCE", ORY_AUDIENCE)
    return {"sub": "ory-user", "iss": ORY_ISSUER, "aud": ORY_AUDIENCE}


@pytest.fixture
def keycloak(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", KEYCLOAK_ISSUER)
    set_client_environment(monkeypatch)
    monkeypatch.setenv("KEYCLOAK_POST_LOGOUT_REDIRECT_URI", KEYCLOAK_REDIRECT)
    return {"sub": "kc-user", "iss": KEYCLOAK_ISSUER, "aud": API_AUDIENCE, "azp": SPA_CLIENT_ID, "typ": "Bearer"}


@pytest.fixture
def zitadel(monkeypatch):
    """The local instance's settings; the claims are the recorded SPA access token's."""
    monkeypatch.setenv("ZITADEL_ISSUER_URL", ZITADEL_ISSUER)
    monkeypatch.setenv("ZITADEL_AUDIENCE", ZITADEL_PROJECT_ID)
    monkeypatch.setenv("ZITADEL_CLIENT_ID", ZITADEL_SPA_CLIENT_ID)
    monkeypatch.setenv("ZITADEL_REQUIRE_HTTPS", "false")
    monkeypatch.setenv("ZITADEL_POST_LOGOUT_REDIRECT_URI", ZITADEL_REDIRECT)
    return json.loads((ZITADEL_FIXTURES / "human-access-token-project.json").read_text())["claims"]


class TestOryResponseIsUnchanged:
    async def test_configured_client_id_and_redirect(self, ory, monkeypatch):
        monkeypatch.setenv("ORY_CLIENT_ID", ORY_CLIENT_ID)
        monkeypatch.setenv("ORY_POST_LOGOUT_REDIRECT_URI", ORY_REDIRECT)

        response, _ = await _logout(ory, ORY_END_SESSION)

        assert response.status_code == 200
        assert response.json() == {
            "status": "logout_redirect",
            "logout_url": (
                "https://ory.example.test/oauth2/sessions/logout"
                "?id_token_hint=the-id-token"
                "&client_id=ory-spa-client"
                "&post_logout_redirect_uri=https%3A%2F%2Fapp.example.test%2Fsigned-out"
            ),
            "sub": "ory-user",
        }

    async def test_defaults_are_the_audience_and_the_frontend_login_page(self, ory, monkeypatch):
        monkeypatch.setenv("FRONTEND_URL", "https://app.example.test/")

        response, _ = await _logout(ory, ORY_END_SESSION, id_token=None)

        assert response.status_code == 200
        assert response.json() == {
            "status": "logout_redirect",
            "logout_url": (
                "https://ory.example.test/oauth2/sessions/logout"
                "?client_id=https%3A%2F%2Fidentity-stack-api"
                "&post_logout_redirect_uri=https%3A%2F%2Fapp.example.test%2Flogin"
            ),
            "sub": "ory-user",
        }

    async def test_empty_settings_fall_back_like_unset_ones(self, ory, monkeypatch):
        monkeypatch.setenv("ORY_CLIENT_ID", "")
        monkeypatch.setenv("ORY_POST_LOGOUT_REDIRECT_URI", "")

        response, _ = await _logout(ory, ORY_END_SESSION, id_token=None)

        assert response.json()["logout_url"] == (
            "https://ory.example.test/oauth2/sessions/logout"
            "?client_id=https%3A%2F%2Fidentity-stack-api"
            "&post_logout_redirect_uri=http%3A%2F%2Flocalhost%3A3000%2Flogin"
        )

    async def test_discovery_keeps_the_strict_policy(self, ory):
        _, disco = await _logout(ory, ORY_END_SESSION)

        request = disco.await_args.args[0]
        assert request.address == f"{ORY_ISSUER}/.well-known/openid-configuration"
        assert request.policy in (None, DiscoveryPolicy())


class TestKeycloakLogout:
    async def test_url_carries_the_spa_client_and_keycloaks_redirect(self, keycloak):
        response, _ = await _logout(keycloak, KEYCLOAK_END_SESSION)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "logout_redirect"
        assert body["sub"] == "kc-user"
        assert body["logout_url"].split("?")[0] == KEYCLOAK_END_SESSION
        assert _query(body["logout_url"]) == {
            "id_token_hint": [ID_TOKEN],
            "client_id": [SPA_CLIENT_ID],
            "post_logout_redirect_uri": [KEYCLOAK_REDIRECT],
        }

    async def test_redirect_defaults_to_the_frontend_login_page(self, keycloak, monkeypatch):
        monkeypatch.delenv("KEYCLOAK_POST_LOGOUT_REDIRECT_URI")
        monkeypatch.setenv("FRONTEND_URL", "https://app.example.test")

        response, _ = await _logout(keycloak, KEYCLOAK_END_SESSION)

        assert _query(response.json()["logout_url"])["post_logout_redirect_uri"] == ["https://app.example.test/login"]

    @pytest.mark.parametrize("azp", [None, "", 7, [SPA_CLIENT_ID], "identity-stack-other"])
    async def test_a_token_without_the_spas_azp_does_not_authenticate(self, keycloak, azp):
        """The token binding refuses it before logout runs: no URL is built and discovery is not asked."""
        claims = {**keycloak, "azp": azp}
        if azp is None:
            del claims["azp"]

        response, disco = await _logout(claims, KEYCLOAK_END_SESSION)

        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid or expired token"}
        disco.assert_not_awaited()

    def test_the_logout_service_names_the_spa_whatever_the_token_says(self, keycloak):
        """The client id is the configured SPA's, never read from the caller's token."""
        (provider,) = [p for p in configured_providers() if p.name == "Keycloak"]

        assert _logout_client_id(provider, {**keycloak, "azp": "identity-stack-other"}) == SPA_CLIENT_ID

    async def test_no_end_session_endpoint_fails_closed(self, keycloak):
        with (
            patch("app.middleware.auth.validate_token", new_callable=AsyncMock) as validate,
            patch("app.services.logout.get_discovery_document", new_callable=AsyncMock) as disco,
        ):
            validate.return_value = keycloak
            disco.return_value = SimpleNamespace(is_successful=False, end_session_endpoint=None)
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
                response = await client.post(
                    "/api/auth/logout", headers={"Authorization": f"Bearer {_jwt(keycloak)}"}, json={}
                )

        assert response.status_code == 500
        assert "logout_url" not in response.json()

    async def test_discovery_is_strict_by_default(self, keycloak):
        _, disco = await _logout(keycloak, KEYCLOAK_END_SESSION)

        request = disco.await_args.args[0]
        assert request.address == f"{KEYCLOAK_ISSUER}/.well-known/openid-configuration"
        assert request.policy in (None, DiscoveryPolicy())

    async def test_discovery_allows_plain_http_only_when_the_switch_is_off(self, keycloak, monkeypatch):
        # Startup allows the switch off only for an issuer on loopback or a ``.localhost`` name.
        local_issuer = "http://keycloak.localhost:8180/realms/identity-stack"
        monkeypatch.setenv("KEYCLOAK_ISSUER_URL", local_issuer)
        monkeypatch.setenv("KEYCLOAK_REQUIRE_HTTPS", "false")

        _, disco = await _logout({**keycloak, "iss": local_issuer}, f"{local_issuer}/protocol/openid-connect/logout")

        assert disco.await_args.args[0].policy == DiscoveryPolicy(require_https=False)


class TestTwoRpInitiatedProviders:
    """Each provider logs out with its own client id and redirect URI."""

    async def test_ory_and_keycloak_do_not_share_settings(self, ory, keycloak, monkeypatch):
        monkeypatch.setenv("ORY_CLIENT_ID", ORY_CLIENT_ID)
        monkeypatch.setenv("ORY_POST_LOGOUT_REDIRECT_URI", ORY_REDIRECT)

        ory_response, _ = await _logout(ory, ORY_END_SESSION)
        keycloak_response, _ = await _logout(keycloak, KEYCLOAK_END_SESSION)

        assert _query(ory_response.json()["logout_url"]) == {
            "id_token_hint": [ID_TOKEN],
            "client_id": [ORY_CLIENT_ID],
            "post_logout_redirect_uri": [ORY_REDIRECT],
        }
        assert _query(keycloak_response.json()["logout_url"]) == {
            "id_token_hint": [ID_TOKEN],
            "client_id": [SPA_CLIENT_ID],
            "post_logout_redirect_uri": [KEYCLOAK_REDIRECT],
        }


class TestZitadelLogout:
    async def test_url_is_the_end_session_endpoint_with_the_hint_the_spa_client_and_the_redirect(self, zitadel):
        response, _ = await _logout(zitadel, ZITADEL_END_SESSION)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "logout_redirect"
        assert body["logout_url"].split("?")[0] == ZITADEL_END_SESSION
        assert _query(body["logout_url"]) == {
            "id_token_hint": [ID_TOKEN],
            "client_id": [ZITADEL_SPA_CLIENT_ID],
            "post_logout_redirect_uri": [ZITADEL_REDIRECT],
        }

    async def test_the_redirect_defaults_to_the_frontend_login_page(self, zitadel, monkeypatch):
        monkeypatch.delenv("ZITADEL_POST_LOGOUT_REDIRECT_URI")
        monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")

        response, _ = await _logout(zitadel, ZITADEL_END_SESSION)

        assert _query(response.json()["logout_url"])["post_logout_redirect_uri"] == ["http://localhost:3000/login"]

    async def test_discovery_takes_the_providers_https_switch(self, zitadel):
        _, disco = await _logout(zitadel, ZITADEL_END_SESSION)

        request = disco.await_args.args[0]
        assert request.address == f"{ZITADEL_ISSUER}/.well-known/openid-configuration"
        assert request.policy == DiscoveryPolicy(require_https=False)

    async def test_the_id_token_itself_cannot_call_logout(self, zitadel):
        id_token_claims = json.loads((ZITADEL_FIXTURES / "human-id-token-project.json").read_text())["claims"]

        response, disco = await _logout(id_token_claims, ZITADEL_END_SESSION)

        assert response.status_code == 401
        assert disco.await_count == 0
