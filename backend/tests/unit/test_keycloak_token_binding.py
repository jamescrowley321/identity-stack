"""Keycloak token binding, run through the real library against the recorded tokens.

Every token here is built from a fixture recorded from Keycloak 26.8.2
(``keycloak/fixtures/``). Only ``iss``, ``iat`` and ``exp`` are re-stamped, so
that a loopback provider (``tests/helpers/local_op.py``) can sign it and the
shipped middlewares can verify it. Every other claim is the one Keycloak issued.

A token is accepted only with the issuer, an RS256 signature, the API audience
in ``aud``, ``azp`` the SPA and ``typ`` ``Bearer``. ``tests/integration/test_keycloak_canonical.py``
runs the same refusals with tokens the compose realm signs.
"""

import base64
import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import (
    KEYCLOAK_MACHINE_CLIENT_PREFIX,
    KeycloakTokenBinding,
    ProviderTokenConfig,
    descope_config,
    keycloak_binding_failure,
    keycloak_config,
    keycloak_machine_client,
)
from tests.helpers.keycloak_model import API_AUDIENCE, OTHER_CLIENT_ID, SPA_CLIENT_ID
from tests.helpers.local_op import ES256, RS256, LocalProvider, local_provider

FIXTURES = Path(__file__).resolve().parents[3] / "keycloak" / "fixtures"
PROBE = "/api/probe"
UNIFORM_401 = {"detail": "Invalid or expired token"}
KEYCLOAK = "Keycloak"
ACCESS = "access-token.json"
ID = "id-token.json"
OTHER = "other-client-access-token.json"


def _claims(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())["claims"]


def _restamped(op: LocalProvider, name: str, **changes) -> dict:
    """The recorded claims of ``name`` on the loopback issuer; ``iat``/``exp`` are set when signed."""
    claims = {key: value for key, value in _claims(name).items() if key not in {"iat", "exp", "nbf"}}
    return {**claims, "iss": op.issuer, **changes}


def _refresh_token_claims(op: LocalProvider) -> dict:
    """A refresh token's claims as Keycloak shapes them: ``typ`` ``Refresh`` and the realm as ``aud``.

    Not recorded (the fixtures hold no refresh token); built from the recorded
    access token, with the SPA's ``azp``, so only ``typ`` and ``aud`` differ.
    """
    return _restamped(op, ACCESS, typ="Refresh", aud=op.issuer)


@pytest.fixture(scope="module")
def op() -> LocalProvider:
    with local_provider() as provider:
        yield provider


def _keycloak(op: LocalProvider) -> ProviderTokenConfig:
    """The shipped Keycloak config on the loopback issuer, which is plain HTTP."""
    return keycloak_config(op.issuer, API_AUDIENCE, SPA_CLIENT_ID, require_https=False)


def _app(middleware, providers: list[ProviderTokenConfig]) -> TestClient:
    app = FastAPI()

    @app.get(PROBE)
    async def probe(request: Request):
        return {"sub": request.state.claims["sub"], "provider": request.state.principal.identity.authentication_type}

    app.add_middleware(middleware, providers=providers)
    return TestClient(app)


def _get(op: LocalProvider, token: str, middleware=TokenValidationMiddleware):
    client = _app(middleware, [descope_config("P-keycloak-binding"), _keycloak(op)])
    return client.get(PROBE, headers={"Authorization": f"Bearer {token}"})


def _assert_refused(response) -> None:
    assert response.status_code == 401
    assert response.json() == UNIFORM_401


MIDDLEWARES = pytest.mark.parametrize(
    "middleware", [TokenValidationMiddleware, GatewayClaimsMiddleware], ids=["standalone", "gateway"]
)


class TestTheRecordedClaimSets:
    """What the binding rests on, as recorded. A re-record that moves any of it fails here."""

    def test_the_spa_access_token(self):
        claims = _claims(ACCESS)

        assert (claims["typ"], claims["azp"], claims["aud"]) == ("Bearer", SPA_CLIENT_ID, API_AUDIENCE)

    def test_the_spa_id_token_names_the_spa_in_azp_and_carries_typ_id(self):
        claims = _claims(ID)

        assert (claims["typ"], claims["azp"], claims["aud"]) == ("ID", SPA_CLIENT_ID, SPA_CLIENT_ID)

    def test_the_other_clients_token_has_no_aud(self):
        claims = _claims(OTHER)

        assert (claims["typ"], claims["azp"], "aud" in claims) == ("Bearer", OTHER_CLIENT_ID, False)

    def test_no_recorded_token_carries_a_role(self):
        for name in (ACCESS, ID, OTHER):
            assert sorted({"resource_access", "realm_access", "roles", "groups"} & set(_claims(name))) == []


class TestTheSpaAccessTokenIsAccepted:
    @MIDDLEWARES
    def test_as_recorded(self, op, middleware):
        response = _get(op, op.token(RS256, _restamped(op, ACCESS)), middleware)

        assert response.status_code == 200
        assert response.json() == {"sub": _claims(ACCESS)["sub"], "provider": KEYCLOAK}

    @MIDDLEWARES
    def test_with_the_api_among_several_audiences(self, op, middleware):
        token = op.token(RS256, _restamped(op, ACCESS, aud=["account", API_AUDIENCE]))

        assert _get(op, token, middleware).status_code == 200


class TestEveryOtherTokenIsRefused:
    @MIDDLEWARES
    def test_the_spas_own_id_token(self, op, middleware):
        _assert_refused(_get(op, op.token(RS256, _restamped(op, ID)), middleware))

    @MIDDLEWARES
    def test_the_id_token_given_the_api_audience(self, op, middleware):
        """Only ``typ`` tells it from the access token: ``azp``, ``sid`` and ``auth_time`` are on both."""
        _assert_refused(_get(op, op.token(RS256, _restamped(op, ID, aud=API_AUDIENCE)), middleware))

    @MIDDLEWARES
    def test_a_refresh_token(self, op, middleware):
        _assert_refused(_get(op, op.token(RS256, _refresh_token_claims(op)), middleware))

    @MIDDLEWARES
    def test_a_refresh_token_given_the_api_audience(self, op, middleware):
        _assert_refused(_get(op, op.token(RS256, _restamped(op, ACCESS, typ="Refresh")), middleware))

    @MIDDLEWARES
    def test_another_clients_token_as_recorded(self, op, middleware):
        _assert_refused(_get(op, op.token(RS256, _restamped(op, OTHER)), middleware))

    @MIDDLEWARES
    def test_another_clients_token_given_the_api_audience(self, op, middleware):
        """Only ``azp`` tells it from the SPA's."""
        _assert_refused(_get(op, op.token(RS256, _restamped(op, OTHER, aud=API_AUDIENCE)), middleware))

    @MIDDLEWARES
    @pytest.mark.parametrize("aud", [None, SPA_CLIENT_ID, "account", [SPA_CLIENT_ID, "account"]])
    def test_the_spas_token_without_the_api_audience(self, op, middleware, aud):
        claims = _restamped(op, ACCESS, aud=aud)
        if aud is None:
            del claims["aud"]

        _assert_refused(_get(op, op.token(RS256, claims), middleware))

    @MIDDLEWARES
    def test_a_token_from_another_issuer_signed_by_the_same_keys(self, op, middleware):
        claims = _restamped(op, ACCESS, iss="http://keycloak-test.localhost:18180/realms/master")

        _assert_refused(_get(op, op.token(RS256, claims), middleware))

    def test_a_token_naming_the_issuer_but_signed_by_another_provider(self, op):
        with local_provider() as other:
            token = other.token(RS256, _restamped(op, ACCESS))

        _assert_refused(_get(op, token))

    @MIDDLEWARES
    def test_a_token_signed_with_another_algorithm(self, op, middleware):
        _assert_refused(_get(op, op.token(ES256, _restamped(op, ACCESS)), middleware))

    @MIDDLEWARES
    @pytest.mark.parametrize("typ", [None, "", "bearer", "ID", ["Bearer"]])
    def test_a_typ_that_is_not_bearer(self, op, middleware, typ):
        claims = _restamped(op, ACCESS, typ=typ)
        if typ is None:
            del claims["typ"]

        _assert_refused(_get(op, op.token(RS256, claims), middleware))

    @MIDDLEWARES
    @pytest.mark.parametrize("azp", [None, "", 7, [SPA_CLIENT_ID], SPA_CLIENT_ID.upper()])
    def test_an_azp_that_is_not_the_spa(self, op, middleware, azp):
        claims = _restamped(op, ACCESS, azp=azp)
        if azp is None:
            del claims["azp"]

        _assert_refused(_get(op, op.token(RS256, claims), middleware))


class TestTheBindingAlone:
    def test_the_spa_access_token_passes(self):
        assert keycloak_binding_failure(SPA_CLIENT_ID, _claims(ACCESS)) is None

    def test_the_id_token_is_named_by_its_typ(self):
        assert keycloak_binding_failure(SPA_CLIENT_ID, _claims(ID)) == "typ is not Bearer"

    def test_the_other_client_is_named_by_its_azp(self):
        assert (
            keycloak_binding_failure(SPA_CLIENT_ID, _claims(OTHER))
            == "azp is neither the SPA client nor a machine credential client"
        )

    def test_a_machine_credential_clients_access_token_passes(self):
        claims = {**_claims(ACCESS), "azp": f"{KEYCLOAK_MACHINE_CLIENT_PREFIX}{uuid.uuid4()}"}

        assert keycloak_binding_failure(SPA_CLIENT_ID, claims) is None

    @pytest.mark.parametrize(
        "azp",
        [KEYCLOAK_MACHINE_CLIENT_PREFIX, f"x{KEYCLOAK_MACHINE_CLIENT_PREFIX}1", "identity-stack-admin"],
        ids=["the bare prefix", "the prefix not at the start", "the admin client"],
    )
    def test_no_other_client_is_a_machine_client(self, azp):
        claims = {**_claims(ACCESS), "azp": azp}

        assert keycloak_machine_client(claims) is None
        assert keycloak_binding_failure(SPA_CLIENT_ID, claims) is not None

    def test_a_machine_clients_id_token_is_still_refused(self):
        claims = {**_claims(ID), "azp": f"{KEYCLOAK_MACHINE_CLIENT_PREFIX}{uuid.uuid4()}"}

        assert keycloak_binding_failure(SPA_CLIENT_ID, claims) == "typ is not Bearer"

    def test_the_reason_quotes_no_claim_value(self):
        claims = {**_claims(ACCESS), "azp": "secret-looking-value", "typ": "secret-looking-type"}

        assert "secret-looking" not in keycloak_binding_failure(SPA_CLIENT_ID, claims)

    def test_the_provider_carries_it(self, op):
        provider = _keycloak(op)

        assert provider.token_binding == KeycloakTokenBinding(SPA_CLIENT_ID)
        assert provider.token_binding(_claims(ID)) is not None
        assert provider.token_binding(_claims(ACCESS)) is None
        assert replace(provider) == provider


def _unsigned(claims: dict) -> str:
    """For the gateway: it reads a payload the gateway verified, so no signature is needed."""

    def segment(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{segment({'alg': RS256})}.{segment(claims)}.sig"


class TestTheGatewayHoldsTheSameRule:
    def test_an_id_token_the_gateway_verified_is_refused(self, op):
        client = _app(GatewayClaimsMiddleware, [_keycloak(op)])
        claims = _restamped(op, ID, aud=API_AUDIENCE, exp=4102444800)

        _assert_refused(client.get(PROBE, headers={"Authorization": f"Bearer {_unsigned(claims)}"}))
