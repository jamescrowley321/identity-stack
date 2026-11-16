"""The Ory provider and the signing algorithm of its tokens, against ``hydra-test``.

The off-set token is issued and signed by Hydra itself: the test has Hydra
generate an ES256 key in its access-token key set through the admin API, so the
key is published in the same JWKS as the RS256 one and Hydra signs the next
access token with it (``OryTokenSource.access_token_signing_key``). Nothing here
signs or edits a token.
"""

import uuid
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import ProviderTokenConfig, ory_config
from tests.helpers.keycloak import decode_unverified
from tests.helpers.ory import OryTokenSource

PROBE = "/api/probe"
UNIFORM_401 = {"detail": "Invalid or expired token"}
RS256 = "RS256"
ES256 = "ES256"
PINNED_TO_RS256 = frozenset({RS256})


@pytest.fixture
def ory(ory_token_source) -> OryTokenSource:
    if ory_token_source.is_live:
        pytest.skip("adds a signing key to the provider; a live Ory project's keys are owner-managed")
    return ory_token_source


def _shipped(ory: OryTokenSource) -> ProviderTokenConfig:
    return ory_config(ory.issuer, ory.audience)


def _mint(ory: OryTokenSource) -> str:
    return ory.mint(sub=f"ory-algorithms-{uuid.uuid4()}", email="algorithms@example.com", email_verified=True)


async def _probe(provider: ProviderTokenConfig, token: str) -> httpx.Response:
    app = FastAPI()

    @app.get(PROBE)
    async def probe(request: Request):
        return {"provider": request.state.principal.identity.authentication_type}

    app.add_middleware(TokenValidationMiddleware, providers=[provider])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(PROBE, headers={"Authorization": f"Bearer {token}"})


def test_the_shipped_config_names_no_algorithms(ory):
    assert _shipped(ory).algorithms == frozenset()


async def test_an_algorithm_outside_the_set_is_refused(ory):
    """Signed ES256 by Hydra, with the key in Hydra's JWKS, refused once the config names RS256."""
    kid = f"es256-{uuid.uuid4()}"
    with ory.access_token_signing_key(ES256, kid):
        token = _mint(ory)
        header, claims = decode_unverified(token)
        assert header["alg"] == ES256
        assert header["kid"] == kid
        assert claims["iss"] == ory.issuer
        jwks = httpx.get(f"{ory.public_url}/.well-known/jwks.json").json()["keys"]
        assert {RS256, ES256} <= {key["alg"] for key in jwks}
        assert kid in {key["kid"] for key in jwks}

        refused = await _probe(replace(_shipped(ory), algorithms=PINNED_TO_RS256), token)
        assert refused.status_code == 401
        assert refused.json() == UNIFORM_401

        # What the shipped config does today: with no algorithms the same token validates.
        unpinned = await _probe(_shipped(ory), token)
        assert unpinned.status_code == 200, unpinned.text


async def test_an_algorithm_inside_the_set_authenticates(ory):
    token = _mint(ory)
    header, _ = decode_unverified(token)
    assert header["alg"] == RS256

    response = await _probe(replace(_shipped(ory), algorithms=PINNED_TO_RS256), token)

    assert response.status_code == 200, response.text
    assert response.json() == {"provider": "Ory"}
