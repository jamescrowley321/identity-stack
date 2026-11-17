"""Zitadel token binding, run through the real library against the recorded tokens.

Every token here is built from a fixture recorded from local Zitadel v4.20.3
(``tests/fixtures/zitadel/``, docs/zitadel.md). Only ``iss``, ``iat`` and
``exp`` are re-stamped, so that a loopback provider (``tests/helpers/local_op.py``)
can sign it and the shipped middleware can verify it through py-identity-model.
Nothing is patched. Every other claim is the one Zitadel issued.

Checks 3 and 4 of the binding rest on claim sets observed on v4.20.3, not on a
documented contract, so ``TestTheRecordedClaimSets`` pins them: a re-record
after a version change that moves one of those claims fails here.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import ProviderTokenConfig, descope_config, zitadel_config
from app.middleware.zitadel import ID_TOKEN_CLAIMS, ORGANIZATION_CLAIM, binding_failure
from tests.helpers.local_op import RS256, LocalProvider, local_provider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "zitadel"
PROBE = "/api/probe"
UNIFORM_401 = {"detail": "Invalid or expired token"}
ZITADEL = "Zitadel"

# The recorded instance's ids. The project id is the last ``aud`` entry of a human
# token; the applications are named in scripts/zitadel-capture-fixtures.py.
PROJECT_ID = "399781183140937731"
SPA_CLIENT_ID = "399781187016540163"  # fixture-jwt-app, the application the SPA stands for
OPAQUE_APP_CLIENT_ID = "399781183577210883"  # fixture-default-app, another application in the project
ROLES_APP_CLIENT_ID = "399781191646986243"  # fixture-jwt-roles-app, another application in the project
MACHINE_CLIENT_ID = "fixture-jwt"


def _recorded(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _claims(name: str) -> dict:
    return _recorded(name)["claims"]


def _restamped(op: LocalProvider, name: str, **changes) -> dict:
    """The recorded claims of ``name`` on the loopback issuer; ``iat``/``exp`` are set when signed."""
    claims = {name_: value for name_, value in _claims(name).items() if name_ not in {"iat", "exp", "nbf"}}
    return {**claims, "iss": op.issuer, **changes}


@pytest.fixture(scope="module")
def op() -> LocalProvider:
    with local_provider() as provider:
        yield provider


def _zitadel(op: LocalProvider) -> ProviderTokenConfig:
    """The shipped Zitadel config on the loopback issuer, which is plain HTTP."""
    return zitadel_config(op.issuer, PROJECT_ID, SPA_CLIENT_ID, require_https=False)


def _get(op: LocalProvider, token: str, providers: list[ProviderTokenConfig] | None = None):
    app = FastAPI()

    @app.get(PROBE)
    async def probe(request: Request):
        return {"sub": request.state.claims["sub"], "provider": request.state.principal.identity.authentication_type}

    app.add_middleware(
        TokenValidationMiddleware,
        providers=providers if providers is not None else [descope_config("P-zitadel-binding"), _zitadel(op)],
    )
    return TestClient(app).get(PROBE, headers={"Authorization": f"Bearer {token}"})


def _assert_refused(response) -> None:
    assert response.status_code == 401
    assert response.json() == UNIFORM_401


class TestTheRecordedClaimSets:
    """What checks 3 and 4 rest on, as recorded. A re-record that moves any of it fails here."""

    def test_the_spa_access_token_carries_client_id_and_no_id_token_claim(self):
        claims = _claims("human-access-token-project.json")

        assert claims["client_id"] == SPA_CLIENT_ID
        assert sorted(set(ID_TOKEN_CLAIMS) & claims.keys()) == []

    def test_the_spa_id_token_has_the_access_tokens_aud_iss_and_typ(self):
        access = _recorded("human-access-token-project.json")
        id_token = _recorded("human-id-token-project.json")

        assert id_token["claims"]["aud"] == access["claims"]["aud"]
        assert id_token["claims"]["iss"] == access["claims"]["iss"]
        assert id_token["header"]["typ"] == access["header"]["typ"]
        assert id_token["claims"]["client_id"] == SPA_CLIENT_ID

    def test_the_spa_id_token_carries_four_of_the_id_token_claims(self):
        claims = _claims("human-id-token-project.json")

        assert sorted(set(ID_TOKEN_CLAIMS) & claims.keys()) == ["at_hash", "auth_time", "azp", "sid"]

    def test_the_other_applications_token_names_the_spa_and_the_project_in_its_aud(self):
        claims = _claims("human-access-token-roles.json")

        assert claims["client_id"] == ROLES_APP_CLIENT_ID
        assert {SPA_CLIENT_ID, PROJECT_ID} <= set(claims["aud"])
        assert sorted(set(ID_TOKEN_CLAIMS) & claims.keys()) == []

    def test_the_machine_token_carries_the_project_audience_alone(self):
        claims = _claims("machine-access-token-project.json")

        assert claims["aud"] == [PROJECT_ID]
        assert claims["client_id"] == MACHINE_CLIENT_ID

    def test_the_default_human_token_is_opaque(self):
        assert _recorded("defaults.json")["oidc_application_human_token"] == {"format": "opaque", "length": 288}

    def test_the_organization_claim_is_on_the_spa_token(self):
        assert ORGANIZATION_CLAIM in _claims("human-access-token-project.json")


class TestTheSpaAccessTokenIsAccepted:
    def test_with_the_organization_scope(self, op):
        response = _get(op, op.token(RS256, _restamped(op, "human-access-token-project.json")))

        assert response.status_code == 200
        assert response.json() == {"sub": _claims("human-access-token-project.json")["sub"], "provider": ZITADEL}

    def test_with_scope_openid_alone(self, op):
        response = _get(op, op.token(RS256, _restamped(op, "human-access-token-openid.json")))

        assert response.status_code == 200
        assert response.json() == {"sub": _claims("human-access-token-openid.json")["sub"], "provider": ZITADEL}


class TestEveryOtherTokenIsRefused:
    def test_another_applications_token_as_recorded(self, op):
        _assert_refused(_get(op, op.token(RS256, _restamped(op, "human-access-token-roles.json"))))

    def test_another_applications_token_with_the_spas_aud_unchanged(self, op):
        """The SPA's own token with only ``client_id`` moved to another application of the project.

        Its ``aud`` is byte-for-byte the SPA token's, so the audience check
        passes it: only the client binding tells them apart.
        """
        claims = _restamped(op, "human-access-token-project.json", client_id=OPAQUE_APP_CLIENT_ID)
        assert OPAQUE_APP_CLIENT_ID in claims["aud"]

        _assert_refused(_get(op, op.token(RS256, claims)))

    def test_the_spas_own_id_token(self, op):
        _assert_refused(_get(op, op.token(RS256, _restamped(op, "human-id-token-project.json"))))

    @pytest.mark.parametrize("claim", ID_TOKEN_CLAIMS)
    def test_an_access_token_carrying_any_one_id_token_claim(self, op, claim):
        claims = _restamped(op, "human-access-token-project.json", **{claim: "present"})

        _assert_refused(_get(op, op.token(RS256, claims)))

    def test_a_machine_user_token_carrying_the_project_audience(self, op):
        _assert_refused(_get(op, op.token(RS256, _restamped(op, "machine-access-token-project.json"))))

    def test_an_opaque_token(self, op):
        length = _recorded("defaults.json")["oidc_application_human_token"]["length"]

        _assert_refused(_get(op, "o" * length))

    def test_a_token_from_another_issuer_signed_by_the_same_keys(self, op):
        claims = _restamped(op, "human-access-token-project.json", iss="http://other.localhost:8480")

        _assert_refused(_get(op, op.token(RS256, claims)))

    def test_a_token_naming_the_issuer_but_signed_by_another_provider(self, op):
        with local_provider() as other:
            token = other.token(RS256, _restamped(op, "human-access-token-project.json"))

        _assert_refused(_get(op, token))

    def test_a_token_without_the_project_audience(self, op):
        claims = _restamped(op, "human-access-token-project.json", aud=[SPA_CLIENT_ID])

        _assert_refused(_get(op, op.token(RS256, claims)))

    def test_a_token_with_no_client_id(self, op):
        claims = _restamped(op, "human-access-token-project.json")
        del claims["client_id"]

        _assert_refused(_get(op, op.token(RS256, claims)))

    def test_the_binding_holds_without_the_descope_provider_beside_it(self, op):
        token = op.token(RS256, _restamped(op, "human-id-token-project.json"))

        _assert_refused(_get(op, token, providers=[_zitadel(op)]))


class TestTheBindingAlone:
    def test_it_names_the_client_it_refused(self):
        claims = {**_claims("human-access-token-project.json"), "client_id": OPAQUE_APP_CLIENT_ID}

        assert binding_failure(SPA_CLIENT_ID, claims) == f"client_id {OPAQUE_APP_CLIENT_ID!r} is not the SPA client"

    @pytest.mark.parametrize("client_id", [None, 42, "", ["399781187016540163"]])
    def test_a_client_id_that_is_not_the_configured_string_is_refused(self, client_id):
        claims = {**_claims("human-access-token-project.json"), "client_id": client_id}

        assert binding_failure(SPA_CLIENT_ID, claims) is not None

    def test_it_names_the_id_token_claims_it_found(self):
        assert binding_failure(SPA_CLIENT_ID, _claims("human-id-token-project.json")) == (
            "carries ID-token claims ['at_hash', 'auth_time', 'azp', 'sid']"
        )

    def test_the_spa_token_passes(self):
        assert binding_failure(SPA_CLIENT_ID, _claims("human-access-token-project.json")) is None

    def test_the_provider_carries_it(self, op):
        provider = _zitadel(op)
        token_binding = provider.token_binding
        assert token_binding is not None

        assert token_binding(_claims("human-id-token-project.json")) is not None
        assert token_binding(_claims("human-access-token-project.json")) is None
        assert replace(provider) == provider
