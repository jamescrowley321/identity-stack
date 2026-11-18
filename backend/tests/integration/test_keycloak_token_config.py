"""Keycloak as a configured provider, against the ``keycloak-test`` container.

Every token is issued and signed by the container, through the realm's
authorization-code flow (``tests/helpers/keycloak.py``). The app is the shipped
middleware and the shipped logout router on the provider list the shared
builder reads from the environment, as ``app.main`` gets it.

The container serves plain HTTP on a name that is not loopback to the library,
so every test that expects a token to validate switches ``KEYCLOAK_REQUIRE_HTTPS``
off, as a local deployment has to.
"""

import time
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from slowapi.errors import RateLimitExceeded

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import KeycloakTokenBinding, ProviderTokenConfig, configured_providers
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.routers import auth as auth_router
from tests.helpers.keycloak import (
    ACCESS_TOKEN_ALG_ATTRIBUTE,
    ACCESS_TOKEN_LIFESPAN_ATTRIBUTE,
    BOB,
    KEYCLOAK_POST_LOGOUT_REDIRECT_URI,
    KeycloakTokens,
    KeycloakTokenSource,
    decode_unverified,
)
from tests.helpers.keycloak_model import (
    API_AUDIENCE,
    KEYCLOAK_VARIABLES,
    OTHER_CLIENT_ID,
    SPA_CLIENT_ID,
    set_client_environment,
)

PROBE = "/api/probe"
LOGOUT = "/api/auth/logout"
UNIFORM_401 = {"detail": "Invalid or expired token"}
KEYCLOAK = "Keycloak"
# Not a real project: the Descope-only app refuses a Keycloak token on its issuer,
# before any request to Descope.
DESCOPE_ONLY_PROJECT_ID = "P2keycloakcrossprovidercheck"
SHORT_LIFESPAN_SECONDS = 1
EXPIRY_MARGIN_SECONDS = 1

_PROVIDER_VARIABLES = (
    "DESCOPE_PROJECT_ID",
    "ORY_ISSUER_URL",
    "ORY_AUDIENCE",
    "ORY_REQUIRE_AUDIENCE",
    "ORY_CLIENT_ID",
    "ORY_POST_LOGOUT_REDIRECT_URI",
    *KEYCLOAK_VARIABLES,
    "FRONTEND_URL",
)


@dataclass(frozen=True)
class KeycloakTokenSet:
    """One login per case, taken once for the module."""

    spa: KeycloakTokens
    other_client: KeycloakTokens
    es256: KeycloakTokens
    expired: KeycloakTokens
    other_realm_access_token: str


@pytest.fixture(scope="module")
def keycloak(keycloak_token_source) -> KeycloakTokenSource:
    return keycloak_token_source


@pytest.fixture(scope="module")
def tokens(keycloak) -> KeycloakTokenSet:
    with keycloak.client_attribute(SPA_CLIENT_ID, ACCESS_TOKEN_ALG_ATTRIBUTE, "ES256"):
        es256 = keycloak.login(BOB, client_id=SPA_CLIENT_ID)
    with keycloak.client_attribute(SPA_CLIENT_ID, ACCESS_TOKEN_LIFESPAN_ATTRIBUTE, str(SHORT_LIFESPAN_SECONDS)):
        expired = keycloak.login(BOB, client_id=SPA_CLIENT_ID)
    token_set = KeycloakTokenSet(
        spa=keycloak.login(BOB, client_id=SPA_CLIENT_ID),
        other_client=keycloak.login(BOB, client_id=OTHER_CLIENT_ID),
        es256=es256,
        expired=expired,
        other_realm_access_token=keycloak.admin_access_token(),
    )
    _, expired_claims = decode_unverified(expired.access_token)
    time.sleep(max(0.0, expired_claims["exp"] + EXPIRY_MARGIN_SECONDS - time.time()))
    return token_set


@pytest.fixture
def environment(monkeypatch, keycloak):
    """A backend environment with Keycloak as the only configured provider."""
    for name in _PROVIDER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", keycloak.issuer)
    set_client_environment(monkeypatch)
    monkeypatch.setenv("KEYCLOAK_REQUIRE_HTTPS", "false")
    monkeypatch.setenv("KEYCLOAK_POST_LOGOUT_REDIRECT_URI", KEYCLOAK_POST_LOGOUT_REDIRECT_URI)
    return monkeypatch


def _app(providers: list[ProviderTokenConfig] | None = None) -> FastAPI:
    """The shipped middleware and logout router on the environment's providers."""
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(auth_router.router, prefix="/api")

    @app.get(PROBE)
    async def probe(request: Request):
        claims = request.state.claims
        return {
            "sub": claims["sub"],
            "iss": claims["iss"],
            "provider": request.state.principal.identity.authentication_type,
        }

    app.add_middleware(TokenValidationMiddleware, providers=providers or configured_providers())
    return app


async def _probe(token: str, providers: list[ProviderTokenConfig] | None = None) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=_app(providers)), base_url="http://test") as client:
        return await client.get(PROBE, headers={"Authorization": f"Bearer {token}"})


def _with_keycloak(**changes) -> list[ProviderTokenConfig]:
    """The environment's providers with the Keycloak entry changed: the control for a refusal."""
    return [replace(p, **changes) if p.name == KEYCLOAK else p for p in configured_providers()]


def _assert_uniform_401(response: httpx.Response) -> None:
    assert response.status_code == 401
    assert response.json() == UNIFORM_401


# --- Done when 1 ---------------------------------------------------------------


async def test_valid_token_authenticates(environment, keycloak, tokens):
    header, claims = decode_unverified(tokens.spa.access_token)
    assert header["alg"] == "RS256"
    assert (claims["aud"], claims["azp"], claims["typ"]) == (API_AUDIENCE, SPA_CLIENT_ID, "Bearer")

    response = await _probe(tokens.spa.access_token)

    assert response.status_code == 200, response.text
    assert response.json() == {"sub": claims["sub"], "iss": keycloak.issuer, "provider": KEYCLOAK}


async def test_wrong_issuer_is_refused(environment, keycloak, tokens):
    """A token the same Keycloak signed for another realm."""
    _, claims = decode_unverified(tokens.other_realm_access_token)
    assert claims["iss"] != keycloak.issuer
    assert claims["iss"].startswith(keycloak.base_url)

    _assert_uniform_401(await _probe(tokens.other_realm_access_token))


async def test_configured_for_another_realm_refuses_this_realms_token(environment, keycloak, tokens):
    environment.setenv("KEYCLOAK_ISSUER_URL", f"{keycloak.base_url}/realms/master")

    _assert_uniform_401(await _probe(tokens.spa.access_token))


async def test_expired_token_is_refused(environment, tokens):
    """Issued with a one-second lifespan, presented after it ran out."""
    _, claims = decode_unverified(tokens.expired.access_token)
    assert claims["aud"] == API_AUDIENCE
    assert claims["exp"] - claims["iat"] == SHORT_LIFESPAN_SECONDS
    assert claims["exp"] < time.time()

    _assert_uniform_401(await _probe(tokens.expired.access_token))


async def test_non_rs256_token_is_refused(environment, keycloak, tokens):
    """Signed ES256 by the realm, with the key in the realm's JWKS."""
    header, claims = decode_unverified(tokens.es256.access_token)
    assert header["alg"] == "ES256"
    assert claims["aud"] == API_AUDIENCE
    jwks = httpx.get(f"{keycloak.issuer}/protocol/openid-connect/certs").json()["keys"]
    assert header["kid"] in {key["kid"] for key in jwks}

    _assert_uniform_401(await _probe(tokens.es256.access_token))

    # Control: the algorithm rule is the only reason. Without it the same token validates.
    unpinned = await _probe(tokens.es256.access_token, _with_keycloak(algorithms=frozenset()))
    assert unpinned.status_code == 200, unpinned.text


async def test_another_clients_token_is_refused(environment, tokens):
    """``identity-stack-other``'s token: the same user and realm, no API audience, its own ``azp``."""
    _, claims = decode_unverified(tokens.other_client.access_token)
    assert "aud" not in claims
    assert (claims["azp"], claims["typ"]) == (OTHER_CLIENT_ID, "Bearer")

    _assert_uniform_401(await _probe(tokens.other_client.access_token))


async def test_a_token_without_the_api_audience_is_refused_for_that_alone(environment, tokens):
    """With the binding moved to the other client, its token still fails: it lacks the API audience."""
    rebound = _with_keycloak(token_binding=KeycloakTokenBinding(OTHER_CLIENT_ID))

    _assert_uniform_401(await _probe(tokens.other_client.access_token, rebound))

    # Control: the audience is the only reason. Not required, the same token validates.
    unaudienced = _with_keycloak(token_binding=KeycloakTokenBinding(OTHER_CLIENT_ID), require_audience=False)
    accepted = await _probe(tokens.other_client.access_token, unaudienced)
    assert accepted.status_code == 200, accepted.text


async def test_id_token_is_not_an_access_token_for_the_api(environment, tokens):
    """The ID token names the SPA in ``aud`` and ``azp`` and says ``typ`` ``ID``."""
    _, claims = decode_unverified(tokens.spa.id_token)
    assert (claims["aud"], claims["azp"], claims["typ"]) == (SPA_CLIENT_ID, SPA_CLIENT_ID, "ID")

    _assert_uniform_401(await _probe(tokens.spa.id_token))


async def test_refresh_token_is_not_an_access_token_for_the_api(environment, tokens):
    _, claims = decode_unverified(tokens.spa.refresh_token)
    assert (claims["azp"], claims["typ"]) == (SPA_CLIENT_ID, "Refresh")

    _assert_uniform_401(await _probe(tokens.spa.refresh_token))


# --- Done when 2 ---------------------------------------------------------------


async def test_https_switch_on_refuses_the_plain_http_issuer(environment, keycloak, tokens):
    assert keycloak.issuer.startswith("http://")
    environment.delenv("KEYCLOAK_REQUIRE_HTTPS")

    _assert_uniform_401(await _probe(tokens.spa.access_token))


async def test_https_switch_off_validates_the_plain_http_issuer(environment, tokens):
    environment.setenv("KEYCLOAK_REQUIRE_HTTPS", "false")

    response = await _probe(tokens.spa.access_token)

    assert response.status_code == 200, response.text


async def test_mistyped_https_switch_stays_on(environment, tokens):
    environment.setenv("KEYCLOAK_REQUIRE_HTTPS", "flase")

    _assert_uniform_401(await _probe(tokens.spa.access_token))


# --- Done when 3 ---------------------------------------------------------------


async def test_descope_token_is_not_accepted_on_the_keycloak_provider(environment, access_token):
    """A live Descope token against a backend whose only issuer allow-list is Keycloak's."""
    _assert_uniform_401(await _probe(access_token))


async def test_keycloak_token_is_not_accepted_on_the_descope_provider(environment, tokens):
    environment.delenv("KEYCLOAK_ISSUER_URL")
    environment.setenv("DESCOPE_PROJECT_ID", DESCOPE_ONLY_PROJECT_ID)
    assert [provider.name for provider in configured_providers()] == ["Descope"]

    _assert_uniform_401(await _probe(tokens.spa.access_token))


async def test_ory_token_is_not_accepted_on_the_keycloak_provider(environment, ory_token_source):
    """A real token from the other local provider, ``hydra-test``."""
    token = ory_token_source.mint(sub="keycloak-cross-provider", email="cross@example.com", email_verified=True)

    _assert_uniform_401(await _probe(token))


async def test_keycloak_token_is_not_accepted_on_the_ory_provider(environment, ory_token_source, tokens):
    environment.delenv("KEYCLOAK_ISSUER_URL")
    environment.setenv("ORY_ISSUER_URL", ory_token_source.issuer)
    environment.setenv("ORY_AUDIENCE", ory_token_source.audience)

    _assert_uniform_401(await _probe(tokens.spa.access_token))


# --- Done when 4 ---------------------------------------------------------------


async def test_logout_returns_the_realms_end_session_url(environment, keycloak, tokens):
    limiter.reset()
    session = keycloak.login(BOB, client_id=SPA_CLIENT_ID)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            LOGOUT,
            headers={"Authorization": f"Bearer {session.access_token}"},
            json={"id_token": session.id_token},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    _, claims = decode_unverified(session.access_token)
    assert body["status"] == "logout_redirect"
    assert body["sub"] == claims["sub"]
    logout_url = urlparse(body["logout_url"])
    assert f"{logout_url.scheme}://{logout_url.netloc}{logout_url.path}" == keycloak.end_session_endpoint
    assert parse_qs(logout_url.query) == {
        "id_token_hint": [session.id_token],
        "client_id": [SPA_CLIENT_ID],
        "post_logout_redirect_uri": [KEYCLOAK_POST_LOGOUT_REDIRECT_URI],
    }

    # Keycloak takes the URL: it ends the session and sends the browser to the
    # registered redirect URI. An unregistered one gets an error page instead.
    followed = httpx.get(body["logout_url"], follow_redirects=False)
    assert followed.status_code == httpx.codes.FOUND, followed.text
    assert followed.headers["location"] == KEYCLOAK_POST_LOGOUT_REDIRECT_URI


async def test_logout_discovery_obeys_the_https_switch(environment, keycloak, tokens):
    """With the switch on, logout does not fetch discovery over plain HTTP."""
    limiter.reset()
    # Authenticated by a provider that may use plain HTTP; logged out by one that may not.
    authenticating = configured_providers()
    environment.delenv("KEYCLOAK_REQUIRE_HTTPS")

    async with AsyncClient(transport=ASGITransport(app=_app(authenticating)), base_url="http://test") as client:
        response = await client.post(
            LOGOUT,
            headers={"Authorization": f"Bearer {tokens.spa.access_token}"},
            json={"id_token": tokens.spa.id_token},
        )

    assert response.status_code == 500
    assert "logout_url" not in response.json()
