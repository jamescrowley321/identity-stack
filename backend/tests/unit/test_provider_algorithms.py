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
from app.middleware.providers import (
    ProviderTokenConfig,
    descope_config,
    keycloak_config,
    library_token_issuers,
    ory_config,
)
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


def _descope_issuers(op: LocalProvider) -> frozenset[str]:
    """Descope's three issuer forms, the two URL forms rooted at the loopback provider.

    The library requires the discovery document's issuer to match the address it
    was fetched from (OpenID Connect Discovery 1.0 §4.3), so the loopback
    provider cannot serve ``https://api.descope.com/{project id}``.
    """
    return frozenset({op.issuer, f"{op.issuer}/v1/apps/{DESCOPE_PROJECT_ID}", DESCOPE_PROJECT_ID})


def _descope(op: LocalProvider, **changes) -> ProviderTokenConfig:
    """The shipped Descope config, its discovery and issuers moved to the loopback provider."""
    return replace(
        descope_config(DESCOPE_PROJECT_ID),
        **{"disco_address": op.disco_address, "accepted_issuers": _descope_issuers(op), **changes},
    )


def _keycloak_claims(op: LocalProvider) -> dict:
    return {"iss": op.issuer, "sub": "kc-user", "aud": KEYCLOAK_AUDIENCE, "azp": "member-app"}


def _descope_claims(op: LocalProvider) -> dict:
    return {"iss": op.issuer, "sub": "descope-user", "aud": DESCOPE_PROJECT_ID}


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
        response = _probe(_descope(op), op.token(RS256, _descope_claims(op)))

        assert response.status_code == 200, response.text
        assert response.json() == {"sub": "descope-user", "provider": "Descope"}

    @pytest.mark.parametrize("alg", [ES256, PS256])
    def test_an_algorithm_outside_the_set_is_refused(self, op, alg):
        """Signed by a key in the same JWKS, refused by the shipped config."""
        token = op.token(alg, _descope_claims(op))

        _assert_uniform_401(_probe(_descope(op), token))

        # Control: the algorithm set is the only reason. Without it the same token validates.
        unpinned = _probe(_descope(op, algorithms=frozenset()), token)
        assert unpinned.status_code == 200, unpinned.text

    @pytest.mark.parametrize("alg", [HS256, UNSIGNED])
    def test_symmetric_and_unsigned_tokens_are_refused(self, op, alg):
        _assert_uniform_401(_probe(_descope(op), op.token(alg, _descope_claims(op))))


class TestDescopeIssuers:
    """Descope signs three ``iss`` forms with one JWKS; the library checks ``iss`` against all three."""

    @pytest.mark.parametrize(
        "claims",
        [
            pytest.param(lambda op: {"iss": op.issuer, "aud": DESCOPE_PROJECT_ID}, id="discovery-issuer"),
            pytest.param(
                lambda op: {"iss": f"{op.issuer}/v1/apps/{DESCOPE_PROJECT_ID}", "aud": DESCOPE_PROJECT_ID},
                id="inbound-app-issuer",
            ),
            pytest.param(lambda op: {"iss": DESCOPE_PROJECT_ID, "drn": "DS"}, id="bare-project-id-session-token"),
        ],
    )
    def test_each_issuer_form_authenticates(self, op, claims):
        response = _probe(_descope(op), op.token(RS256, {**claims(op), "sub": "descope-user"}))

        assert response.status_code == 200, response.text
        assert response.json() == {"sub": "descope-user", "provider": "Descope"}

    def test_an_issuer_outside_the_set_is_refused(self, op):
        claims = {**_descope_claims(op), "iss": f"{op.issuer}/v1/apps/P2someoneelsesproject"}

        _assert_uniform_401(_probe(_descope(op), op.token(RS256, claims)))

    def test_keys_from_a_discovery_document_naming_another_issuer_are_not_used(self, op):
        """The listed issuers are accepted only from the keys of a discovery document whose issuer is listed."""
        token = op.token(RS256, {**_descope_claims(op), "iss": DESCOPE_PROJECT_ID, "drn": "DS"})
        elsewhere = _descope(op, accepted_issuers=frozenset({DESCOPE_ISSUER, DESCOPE_PROJECT_ID}))

        _assert_uniform_401(_probe(elsewhere, token))

        # Control: the same token is accepted once the discovery issuer is listed.
        assert _probe(_descope(op), token).status_code == 200

    async def test_the_issuer_check_is_the_library_s(self, op):
        """The issuer list reaches py-identity-model, which refuses an unlisted ``iss`` itself."""
        config = TokenValidationConfig(
            perform_disco=True,
            token_issuers=library_token_issuers(_descope(op)),
            options={"verify_aud": False},
        )
        claims = {**_descope_claims(op), "iss": f"{op.issuer}/v1/apps/P2someoneelsesproject"}

        with pytest.raises(TokenValidationException, match="[Ii]ssuer"):
            await validate_token(
                jwt=op.token(RS256, claims),
                token_validation_config=config,
                disco_doc_address=op.disco_address,
            )


class TestLibraryTokenIssuers:
    def test_descope_lists_its_three_issuer_forms(self):
        assert library_token_issuers(descope_config(DESCOPE_PROJECT_ID)) == sorted(
            [DESCOPE_PROJECT_ID, DESCOPE_ISSUER, f"https://api.descope.com/v1/apps/{DESCOPE_PROJECT_ID}"]
        )

    def test_descope_without_a_project_id_lists_none(self):
        assert library_token_issuers(descope_config("")) is None

    def test_a_provider_with_one_issuer_lists_none(self, op):
        assert library_token_issuers(ory_config("https://ory.example.test", "api")) is None
        assert library_token_issuers(_keycloak(op)) is None


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
            options={"verify_aud": False},
        )

        with pytest.raises(TokenValidationException, match=f"Algorithm '{alg}' is not in the allowed algorithms"):
            await validate_token(
                jwt=op.token(alg, _descope_claims(op)),
                token_validation_config=config,
                disco_doc_address=op.disco_address,
            )

    def test_a_dpop_bound_token_sent_as_a_bearer_token_is_refused(self, op):
        """RFC 9449 §7.2. The middleware passes no proof, so a bound token never authenticates."""
        bound = {**_descope_claims(op), "cnf": {"jkt": DPOP_KEY_THUMBPRINT}}

        _assert_uniform_401(_probe(_descope(op), op.token(RS256, bound)))

        # Control: the confirmation claim is the only reason.
        unbound = _probe(_descope(op), op.token(RS256, _descope_claims(op)))
        assert unbound.status_code == 200, unbound.text

    @pytest.mark.parametrize("alg", [HS256, UNSIGNED])
    def test_a_provider_that_names_no_set_still_refuses_symmetric_and_unsigned_tokens(self, op, alg):
        """With no set named the library accepts its asymmetric defaults and nothing else."""
        unpinned = _descope(op, algorithms=frozenset())

        _assert_uniform_401(_probe(unpinned, op.token(alg, _descope_claims(op))))
