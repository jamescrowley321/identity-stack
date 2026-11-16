"""The provider's ``algorithms`` set, enforced by py-identity-model in standalone mode.

Nothing is patched: a loopback provider (``tests/helpers/local_op.py``) serves
discovery and a JWKS with an RS256, an ES256 and a PS256 key, and the shipped
middleware validates tokens signed by those keys through the real library.

Descope has no local IdP, so its config is tested here with only the discovery
address pointed at the loopback provider.
"""

from dataclasses import replace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import ProviderTokenConfig, descope_config, keycloak_config
from tests.helpers.local_op import ES256, HS256, PS256, RS256, UNSIGNED, LocalProvider, local_provider

PROBE = "/api/probe"
UNIFORM_401 = {"detail": "Invalid or expired token"}
KEYCLOAK_AUDIENCE = "member"
DESCOPE_PROJECT_ID = "P2algorithmallowlistcheck"
DESCOPE_ISSUER = f"https://api.descope.com/{DESCOPE_PROJECT_ID}"
PINNED_TO_RS256 = frozenset({RS256})


@pytest.fixture(scope="module")
def op() -> LocalProvider:
    with local_provider() as provider:
        yield provider


def _keycloak(op: LocalProvider, **changes) -> ProviderTokenConfig:
    return replace(keycloak_config(op.issuer, frozenset({KEYCLOAK_AUDIENCE})), **changes)


def _descope(op: LocalProvider, **changes) -> ProviderTokenConfig:
    """The shipped Descope config, fetching discovery and JWKS from the loopback provider."""
    return replace(descope_config(DESCOPE_PROJECT_ID), disco_address=op.disco_address, **changes)


def _keycloak_claims(op: LocalProvider) -> dict:
    return {"iss": op.issuer, "sub": "kc-user", "aud": KEYCLOAK_AUDIENCE, "azp": "member-app"}


def _descope_claims() -> dict:
    return {"iss": DESCOPE_ISSUER, "sub": "descope-user", "aud": DESCOPE_PROJECT_ID}


def _probe(provider: ProviderTokenConfig, token: str):
    app = FastAPI()

    @app.get(PROBE)
    async def probe(request: Request):
        return {"sub": request.state.claims["sub"], "provider": request.state.principal.identity.authentication_type}

    app.add_middleware(TokenValidationMiddleware, providers=[provider])
    return TestClient(app).get(PROBE, headers={"Authorization": f"Bearer {token}"})


def _assert_uniform_401(response) -> None:
    assert response.status_code == 401
    assert response.json() == UNIFORM_401


class TestKeycloak:
    def test_rs256_token_authenticates(self, op):
        response = _probe(_keycloak(op), op.token(RS256, _keycloak_claims(op)))

        assert response.status_code == 200, response.text
        assert response.json() == {"sub": "kc-user", "provider": "Keycloak"}

    @pytest.mark.parametrize("alg", [ES256, PS256])
    def test_non_rs256_is_refused_even_if_the_signature_would_verify(self, op, alg):
        token = op.token(alg, _keycloak_claims(op))

        _assert_uniform_401(_probe(_keycloak(op), token))

        # Control: the algorithm set is the only reason. Without it the same token validates.
        unpinned = _probe(_keycloak(op, algorithms=frozenset()), token)
        assert unpinned.status_code == 200, unpinned.text

    @pytest.mark.parametrize("alg", [HS256, UNSIGNED])
    def test_symmetric_and_unsigned_tokens_are_refused(self, op, alg):
        _assert_uniform_401(_probe(_keycloak(op), op.token(alg, _keycloak_claims(op))))

    def test_a_header_that_cannot_be_read_is_refused(self, op):
        _, payload, signature = op.token(RS256, _keycloak_claims(op)).split(".")

        _assert_uniform_401(_probe(_keycloak(op), f"!!!.{payload}.{signature}"))


class TestDescope:
    def test_the_shipped_config_names_no_algorithms(self):
        assert descope_config(DESCOPE_PROJECT_ID).algorithms == frozenset()

    @pytest.mark.parametrize("alg", [RS256, ES256, PS256])
    def test_with_no_algorithms_any_key_in_the_jwks_authenticates(self, op, alg):
        """What the shipped config does today: the JWKS decides the algorithm."""
        response = _probe(_descope(op), op.token(alg, _descope_claims()))

        assert response.status_code == 200, response.text
        assert response.json() == {"sub": "descope-user", "provider": "Descope"}

    @pytest.mark.parametrize("alg", [ES256, PS256])
    def test_an_algorithm_outside_the_set_is_refused(self, op, alg):
        """Signed by a key in the same JWKS, refused once the config names RS256."""
        _assert_uniform_401(_probe(_descope(op, algorithms=PINNED_TO_RS256), op.token(alg, _descope_claims())))

    def test_an_algorithm_inside_the_set_authenticates(self, op):
        response = _probe(_descope(op, algorithms=PINNED_TO_RS256), op.token(RS256, _descope_claims()))

        assert response.status_code == 200, response.text
