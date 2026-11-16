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
from py_identity_model import TokenValidationConfig, TokenValidationException
from py_identity_model.aio import validate_token

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import ProviderTokenConfig, descope_config, keycloak_config, ory_config
from tests.helpers.keycloak_model import model_settings
from tests.helpers.local_op import ES256, HS256, PS256, RS256, UNSIGNED, LocalProvider, local_provider

PROBE = "/api/probe"
UNIFORM_401 = {"detail": "Invalid or expired token"}
KEYCLOAK_AUDIENCE = "member"
DESCOPE_PROJECT_ID = "P2algorithmallowlistcheck"
DESCOPE_ISSUER = f"https://api.descope.com/{DESCOPE_PROJECT_ID}"
PINNED_TO_RS256 = frozenset({RS256})
# The JWK thumbprint of the example proof key in RFC 9449 §6.1.
DPOP_KEY_THUMBPRINT = "0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I"


@pytest.fixture(scope="module")
def op() -> LocalProvider:
    with local_provider() as provider:
        yield provider


def _keycloak(op: LocalProvider, **changes) -> ProviderTokenConfig:
    return replace(
        keycloak_config(op.issuer, frozenset({KEYCLOAK_AUDIENCE}), **model_settings(KEYCLOAK_AUDIENCE)), **changes
    )


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
    def test_the_shipped_config_accepts_rs256_only(self):
        assert descope_config(DESCOPE_PROJECT_ID).algorithms == PINNED_TO_RS256

    def test_an_rs256_token_authenticates(self, op):
        response = _probe(_descope(op), op.token(RS256, _descope_claims()))

        assert response.status_code == 200, response.text
        assert response.json() == {"sub": "descope-user", "provider": "Descope"}

    @pytest.mark.parametrize("alg", [ES256, PS256])
    def test_an_algorithm_outside_the_set_is_refused(self, op, alg):
        """Signed by a key in the same JWKS, refused by the shipped config."""
        token = op.token(alg, _descope_claims())

        _assert_uniform_401(_probe(_descope(op), token))

        # Control: the algorithm set is the only reason. Without it the same token validates.
        unpinned = _probe(_descope(op, algorithms=frozenset()), token)
        assert unpinned.status_code == 200, unpinned.text

    @pytest.mark.parametrize("alg", [HS256, UNSIGNED])
    def test_symmetric_and_unsigned_tokens_are_refused(self, op, alg):
        _assert_uniform_401(_probe(_descope(op), op.token(alg, _descope_claims())))


class TestOry:
    """The config only: ``tests/integration/test_ory_algorithms.py`` has Hydra sign the tokens."""

    def test_the_shipped_config_accepts_rs256_only(self):
        assert ory_config("https://ory.example.test", "api").algorithms == PINNED_TO_RS256


class TestTheLibraryRefuses:
    """The refusals above come from py-identity-model, not from a check in the middleware."""

    @pytest.mark.parametrize("alg", [ES256, PS256])
    async def test_a_named_set_refuses_an_algorithm_outside_it(self, op, alg):
        config = TokenValidationConfig(
            perform_disco=True,
            algorithms=[RS256],
            options={"verify_iss": False, "verify_aud": False},
        )

        with pytest.raises(TokenValidationException, match=f"Algorithm '{alg}' is not in the allowed algorithms"):
            await validate_token(
                jwt=op.token(alg, _descope_claims()),
                token_validation_config=config,
                disco_doc_address=op.disco_address,
            )

    def test_a_dpop_bound_token_sent_as_a_bearer_token_is_refused(self, op):
        """RFC 9449 §7.2. The middleware passes no proof, so a bound token never authenticates."""
        bound = {**_descope_claims(), "cnf": {"jkt": DPOP_KEY_THUMBPRINT}}

        _assert_uniform_401(_probe(_descope(op), op.token(RS256, bound)))

        # Control: the confirmation claim is the only reason.
        unbound = _probe(_descope(op), op.token(RS256, _descope_claims()))
        assert unbound.status_code == 200, unbound.text

    @pytest.mark.parametrize("alg", [HS256, UNSIGNED])
    def test_a_provider_that_names_no_set_still_refuses_symmetric_and_unsigned_tokens(self, op, alg):
        """With no set named the library accepts its asymmetric defaults and nothing else."""
        unpinned = _descope(op, algorithms=frozenset())

        _assert_uniform_401(_probe(unpinned, op.token(alg, _descope_claims())))
