"""``POST /api/validate-id-token``: the provider whose issuer the token names, with no middleware in front.

The route is excluded from both token middlewares. It takes the configured
provider one of whose ID-token issuers is the token's unverified ``iss`` and
validates the token as an OIDC ID token with that provider's discovery
address, algorithms and client id, answering a refused token with the same 401
body as the middlewares.

Nothing in the library is patched: each provider is a loopback provider
(``tests/helpers/local_op.py``) serving discovery and a JWKS, on an issuer of its
own. The shipped configs are pointed at them; Descope's two URL issuers are moved
to two loopback providers, each serving its own discovery document, as
``api.descope.com`` serves one per URL issuer. The Keycloak and
Zitadel tokens are the recorded ones, re-stamped on the loopback issuer.
"""

import base64
import json
import socket
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.middleware import providers as provider_registry
from app.middleware.providers import (
    ProviderTokenConfig,
    descope_config,
    id_token_client_id,
    id_token_disco_address,
    id_token_issuers,
    keycloak_config,
    ory_config,
    select_id_token_provider,
    zitadel_config,
)
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.routers import protected
from tests.helpers.keycloak_model import API_AUDIENCE, OTHER_CLIENT_ID, SPA_CLIENT_ID
from tests.helpers.local_op import ES256, HS256, PS256, RS256, UNSIGNED, LocalProvider, local_provider

ROUTE = "/api/validate-id-token"
ROUTE_LOGGER = "app.routers.protected"
UNIFORM_401 = {"detail": "Invalid or expired token"}
PROVIDER_UNAVAILABLE = {"detail": "Identity provider unavailable"}
CALLER_CHOSEN_ALG = "CALLER-CHOSEN-ALG-7f3a"

DESCOPE = "Descope"
DESCOPE_INBOUND_APP = "Descope inbound app"
KEYCLOAK = "Keycloak"
ZITADEL = "Zitadel"
ORY = "Ory"
DESCOPE_PROJECT_ID = "P2validateidtokencheck"
ORY_AUDIENCE = "ory-api"
ORY_CLIENT_ID = "ory-spa"

_ROOT = Path(__file__).resolve().parents[3]
KEYCLOAK_FIXTURES = _ROOT / "keycloak" / "fixtures"
ZITADEL_FIXTURES = _ROOT / "backend" / "tests" / "fixtures" / "zitadel"
# The recorded Zitadel instance's project and SPA application (test_zitadel_token_binding.py).
ZITADEL_PROJECT_ID = "399781183140937731"
ZITADEL_SPA_CLIENT_ID = "399781187016540163"
ZITADEL_OTHER_APP_CLIENT_ID = "399781183577210883"

REFUSED_UNKNOWN_ISSUER = ("WARNING", "ID validation refused: UNKNOWN_ISSUER")
REFUSED_ACCESS_TOKEN = ("WARNING", "ID validation refused: CARRIES_SCOPE")


def _recorded(directory: Path, name: str, op: LocalProvider, **changes) -> dict:
    """The recorded claims of ``name`` on ``op``'s issuer; ``iat``/``exp`` are set when signed."""
    claims = json.loads((directory / name).read_text())["claims"]
    kept = {key: value for key, value in claims.items() if key not in {"iat", "exp", "nbf"}}
    return {**kept, "iss": op.issuer, **changes}


def _descope(op: LocalProvider, inbound_app: LocalProvider | None = None, **changes) -> ProviderTokenConfig:
    """The shipped Descope config, its discovery and URL issuers moved to loopback providers.

    The library requires the discovery document's issuer to match the address it
    was fetched from, so a loopback provider cannot serve ``https://api.descope.com/{pid}``.
    ``op`` stands for that issuer and ``inbound_app`` for ``https://api.descope.com/v1/apps/{pid}``.
    """
    second = inbound_app.issuer if inbound_app is not None else f"{op.issuer}/v1/apps/{DESCOPE_PROJECT_ID}"
    issuers = frozenset({op.issuer, second, DESCOPE_PROJECT_ID})
    return replace(
        descope_config(DESCOPE_PROJECT_ID),
        **{"disco_address": op.disco_address, "accepted_issuers": issuers, **changes},
    )


def _keycloak(op: LocalProvider) -> ProviderTokenConfig:
    return keycloak_config(op.issuer, API_AUDIENCE, SPA_CLIENT_ID, require_https=False)


def _zitadel(op: LocalProvider) -> ProviderTokenConfig:
    return zitadel_config(op.issuer, ZITADEL_PROJECT_ID, ZITADEL_SPA_CLIENT_ID, require_https=False)


def _ory(op: LocalProvider) -> ProviderTokenConfig:
    return ory_config(op.issuer, ORY_AUDIENCE, client_id=ORY_CLIENT_ID)


def _hydra_access_token_claims(op: LocalProvider, aud: list[str]) -> dict:
    """An Ory Hydra JWT access token: its scopes in ``scp``, the client in ``client_id``, no ``azp``."""
    return {
        "iss": op.issuer,
        "sub": "ory-user",
        "aud": aud,
        "client_id": ORY_CLIENT_ID,
        "scp": ["openid", "offline_access"],
        "jti": "6c5a8b2e-hydra",
        "ext": {},
    }


class Deployment:
    """Four configured providers, each on a loopback provider of its own."""

    def __init__(self, ops: dict[str, LocalProvider]) -> None:
        self.ops = ops
        self.providers = {
            DESCOPE: _descope(ops[DESCOPE], ops[DESCOPE_INBOUND_APP]),
            KEYCLOAK: _keycloak(ops[KEYCLOAK]),
            ZITADEL: _zitadel(ops[ZITADEL]),
            ORY: _ory(ops[ORY]),
        }

    def id_token_claims(self, name: str) -> dict:
        op = self.ops[name]
        if name == KEYCLOAK:
            return _recorded(KEYCLOAK_FIXTURES, "id-token.json", op)
        if name == ZITADEL:
            return _recorded(ZITADEL_FIXTURES, "human-id-token-project.json", op)
        if name == DESCOPE:
            return {"iss": op.issuer, "sub": "descope-user", "aud": DESCOPE_PROJECT_ID, "azp": DESCOPE_PROJECT_ID}
        return {"iss": op.issuer, "sub": "ory-user", "aud": [ORY_CLIENT_ID], "azp": ORY_CLIENT_ID}

    def access_token_claims(self, name: str) -> dict:
        op = self.ops[name]
        if name == KEYCLOAK:
            return _recorded(KEYCLOAK_FIXTURES, "access-token.json", op)
        if name == ZITADEL:
            return _recorded(ZITADEL_FIXTURES, "human-access-token-project.json", op)
        if name == DESCOPE:
            # A Descope access token as minted for the project's OIDC application:
            # the project id as ``aud`` and ``azp``, and a ``scope``.
            return {
                "iss": op.issuer,
                "sub": "descope-user",
                "aud": [DESCOPE_PROJECT_ID],
                "azp": DESCOPE_PROJECT_ID,
                "scope": "openid",
            }
        return _hydra_access_token_claims(op, [ORY_AUDIENCE])


@pytest.fixture(scope="module")
def deployment() -> Deployment:
    with (
        local_provider() as descope_op,
        local_provider() as descope_inbound_app_op,
        local_provider() as keycloak_op,
        local_provider() as zitadel_op,
        local_provider() as ory_op,
    ):
        yield Deployment(
            {
                DESCOPE: descope_op,
                DESCOPE_INBOUND_APP: descope_inbound_app_op,
                KEYCLOAK: keycloak_op,
                ZITADEL: zitadel_op,
                ORY: ory_op,
            }
        )


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    limiter.reset()
    yield
    limiter.reset()


def _closed_port() -> int:
    """A loopback port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _client(monkeypatch, providers: list[ProviderTokenConfig]) -> TestClient:
    """The route alone, on a deployment configured with ``providers``."""
    monkeypatch.setattr(protected, "configured_providers", lambda: list(providers))
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(protected.router, prefix="/api")
    return TestClient(app)


def _deployed(monkeypatch, deployment: Deployment) -> TestClient:
    return _client(monkeypatch, list(deployment.providers.values()))


def _post(client: TestClient, token: str):
    return client.post(ROUTE, headers={"Authorization": f"Bearer {token}"})


def _assert_uniform_401(response) -> None:
    assert response.status_code == 401, response.text
    assert response.json() == UNIFORM_401


def _route_records(caplog) -> list[tuple[str, str]]:
    """Level and message of each record the route's own logger wrote."""
    return [(record.levelname, record.getMessage()) for record in caplog.records if record.name == ROUTE_LOGGER]


def _tampered(token: str, **changes) -> str:
    """``token`` with its payload changed and its signature kept."""
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    changed = base64.urlsafe_b64encode(json.dumps({**claims, **changes}).encode()).decode().rstrip("=")
    return f"{header}.{changed}.{signature}"


ALL_PROVIDERS = [DESCOPE, KEYCLOAK, ZITADEL, ORY]


class TestProviderSelection:
    def test_the_route_reads_the_middlewares_provider_registry(self):
        assert protected.configured_providers is provider_registry.configured_providers

    def test_descope_id_tokens_carry_its_two_url_issuers_not_the_bare_project_id(self):
        assert id_token_issuers(descope_config(DESCOPE_PROJECT_ID)) == {
            f"https://api.descope.com/{DESCOPE_PROJECT_ID}",
            f"https://api.descope.com/v1/apps/{DESCOPE_PROJECT_ID}",
        }

    def test_a_descope_id_token_is_validated_with_its_own_issuers_discovery_document(self):
        descope = descope_config(DESCOPE_PROJECT_ID)
        issuers = sorted(id_token_issuers(descope))

        assert {iss: id_token_disco_address(descope, iss) for iss in issuers} == {
            iss: f"{iss}/.well-known/openid-configuration" for iss in issuers
        }

    def test_another_providers_discovery_address_is_its_configured_one(self, deployment):
        keycloak = deployment.providers[KEYCLOAK]

        assert id_token_disco_address(keycloak, deployment.ops[KEYCLOAK].issuer) == keycloak.disco_address

    def test_descope_with_no_project_id_has_no_id_token_issuer(self):
        assert select_id_token_provider([descope_config("")], "") is None

    def test_the_client_id_is_the_spas(self, deployment):
        assert {name: id_token_client_id(provider) for name, provider in deployment.providers.items()} == {
            DESCOPE: DESCOPE_PROJECT_ID,
            KEYCLOAK: SPA_CLIENT_ID,
            ZITADEL: ZITADEL_SPA_CLIENT_ID,
            ORY: ORY_CLIENT_ID,
        }

    def test_a_provider_is_selected_by_its_issuer(self, deployment):
        providers = list(deployment.providers.values())
        assert {name: select_id_token_provider(providers, op.issuer) for name, op in deployment.ops.items()} == (
            deployment.providers | {DESCOPE_INBOUND_APP: deployment.providers[DESCOPE]}
        )


class TestEachProvidersIdToken:
    @pytest.mark.parametrize("name", ALL_PROVIDERS)
    def test_is_validated_and_its_claims_returned(self, monkeypatch, deployment, name):
        claims = deployment.id_token_claims(name)

        response = _post(_deployed(monkeypatch, deployment), deployment.ops[name].token(RS256, claims))

        assert response.status_code == 200, response.text
        returned = response.json()
        assert {key: returned[key] for key in ("iss", "sub", "aud")} == {
            key: claims[key] for key in ("iss", "sub", "aud")
        }

    def test_a_descope_id_token_on_the_inbound_app_issuer(self, monkeypatch, deployment):
        op = deployment.ops[DESCOPE_INBOUND_APP]
        claims = {**deployment.id_token_claims(DESCOPE), "iss": op.issuer}

        response = _post(_deployed(monkeypatch, deployment), op.token(RS256, claims))

        assert response.status_code == 200, response.text
        assert response.json()["iss"] == claims["iss"]


class TestRefused:
    @pytest.mark.parametrize("name", ALL_PROVIDERS)
    def test_an_access_token(self, monkeypatch, deployment, name):
        token = deployment.ops[name].token(RS256, deployment.access_token_claims(name))

        _assert_uniform_401(_post(_deployed(monkeypatch, deployment), token))

    def test_an_access_token_that_passes_the_id_token_profile_is_refused_for_its_scope(
        self, monkeypatch, deployment, caplog
    ):
        token = deployment.ops[DESCOPE].token(RS256, deployment.access_token_claims(DESCOPE))

        with caplog.at_level("DEBUG"):
            _assert_uniform_401(_post(_deployed(monkeypatch, deployment), token))

        assert _route_records(caplog) == [REFUSED_ACCESS_TOKEN]

    def test_an_ory_access_token_naming_the_client_is_refused_for_its_scp(self, monkeypatch, deployment, caplog):
        """Passes the ID-token profile (``aud`` the client, no ``azp``); only ``scp`` marks it."""
        op = deployment.ops[ORY]
        token = op.token(RS256, _hydra_access_token_claims(op, [ORY_CLIENT_ID]))

        with caplog.at_level("DEBUG"):
            _assert_uniform_401(_post(_deployed(monkeypatch, deployment), token))

        assert _route_records(caplog) == [REFUSED_ACCESS_TOKEN]

    def test_a_descope_session_token_on_the_bare_project_id(self, monkeypatch, deployment, caplog):
        claims = {"iss": DESCOPE_PROJECT_ID, "sub": "descope-user", "drn": "DS"}
        token = deployment.ops[DESCOPE].token(RS256, claims)

        with caplog.at_level("DEBUG"):
            _assert_uniform_401(_post(_deployed(monkeypatch, deployment), token))

        assert _route_records(caplog) == [REFUSED_UNKNOWN_ISSUER]

    def test_a_token_of_an_issuer_no_provider_has(self, monkeypatch, deployment, caplog):
        op = deployment.ops[KEYCLOAK]
        token = op.token(RS256, deployment.id_token_claims(KEYCLOAK) | {"iss": f"{op.issuer}/realms/another"})

        with caplog.at_level("DEBUG"):
            _assert_uniform_401(_post(_deployed(monkeypatch, deployment), token))

        assert _route_records(caplog) == [REFUSED_UNKNOWN_ISSUER]

    def test_a_token_naming_one_providers_issuer_signed_by_another(self, monkeypatch, deployment):
        token = deployment.ops[ZITADEL].token(RS256, deployment.id_token_claims(KEYCLOAK))

        _assert_uniform_401(_post(_deployed(monkeypatch, deployment), token))

    @pytest.mark.parametrize("name", ALL_PROVIDERS)
    def test_a_tampered_token(self, monkeypatch, deployment, name):
        token = deployment.ops[name].token(RS256, deployment.id_token_claims(name))

        _assert_uniform_401(_post(_deployed(monkeypatch, deployment), _tampered(token, sub="someone-else")))

    def test_an_id_token_of_another_client(self, monkeypatch, deployment):
        claims = deployment.id_token_claims(KEYCLOAK) | {"aud": OTHER_CLIENT_ID, "azp": OTHER_CLIENT_ID}

        _assert_uniform_401(_post(_deployed(monkeypatch, deployment), deployment.ops[KEYCLOAK].token(RS256, claims)))

    def test_a_zitadel_id_token_of_another_application_of_the_project(self, monkeypatch, deployment):
        """Its ``aud`` is the SPA's ID token's; only ``azp`` names another application."""
        claims = deployment.id_token_claims(ZITADEL) | {
            "azp": ZITADEL_OTHER_APP_CLIENT_ID,
            "client_id": ZITADEL_OTHER_APP_CLIENT_ID,
        }

        _assert_uniform_401(_post(_deployed(monkeypatch, deployment), deployment.ops[ZITADEL].token(RS256, claims)))

    def test_an_id_token_carrying_a_nonce(self, monkeypatch, deployment):
        """The SPA sends no nonce, so a token carrying one was not issued to its sign-in."""
        claims = deployment.id_token_claims(KEYCLOAK) | {"nonce": "n-0S6_WzA2Mj"}

        _assert_uniform_401(_post(_deployed(monkeypatch, deployment), deployment.ops[KEYCLOAK].token(RS256, claims)))

    def test_an_expired_token(self, monkeypatch, deployment):
        claims = deployment.id_token_claims(KEYCLOAK) | {"exp": 1}

        _assert_uniform_401(_post(_deployed(monkeypatch, deployment), deployment.ops[KEYCLOAK].token(RS256, claims)))

    @pytest.mark.parametrize("alg", [ES256, PS256])
    def test_an_algorithm_outside_the_providers_set(self, monkeypatch, deployment, alg):
        """Signed by a key in the same JWKS, refused by the shipped config."""
        op = deployment.ops[DESCOPE]
        token = op.token(alg, deployment.id_token_claims(DESCOPE))

        _assert_uniform_401(_post(_deployed(monkeypatch, deployment), token))

        # Control: the algorithm set is the only reason. Without it the same token validates.
        unpinned = _post(_client(monkeypatch, [_descope(op, algorithms=frozenset())]), token)
        assert unpinned.status_code == 200, unpinned.text

    @pytest.mark.parametrize("alg", [HS256, UNSIGNED])
    def test_symmetric_and_unsigned_tokens(self, monkeypatch, deployment, alg):
        token = deployment.ops[DESCOPE].token(alg, deployment.id_token_claims(DESCOPE))

        _assert_uniform_401(_post(_deployed(monkeypatch, deployment), token))

    @pytest.mark.parametrize(
        "token",
        ["not-a-jwt", "a.b.c", ""],
        ids=["opaque", "three-segments-of-nothing", "empty"],
    )
    def test_a_token_that_is_not_a_jwt(self, monkeypatch, deployment, token):
        _assert_uniform_401(_post(_deployed(monkeypatch, deployment), token))


class TestLogging:
    def test_a_refusal_is_one_log_line_naming_the_error_type(self, monkeypatch, deployment, caplog):
        token = deployment.ops[DESCOPE].token(ES256, deployment.id_token_claims(DESCOPE))

        with caplog.at_level("DEBUG"):
            _assert_uniform_401(_post(_deployed(monkeypatch, deployment), token))

        assert _route_records(caplog) == [("WARNING", "ID validation refused: TokenValidationException")]
        assert token not in caplog.text

    def test_a_refusal_logs_no_traceback_and_nothing_the_caller_chose(self, monkeypatch, deployment, caplog):
        """The header ``alg`` is the caller's text, and the library quotes it in its error."""
        token = deployment.ops[DESCOPE].token_with_header_alg(CALLER_CHOSEN_ALG, deployment.id_token_claims(DESCOPE))

        with caplog.at_level("DEBUG"):
            _assert_uniform_401(_post(_deployed(monkeypatch, deployment), token))

        ours = [record for record in caplog.records if record.name == ROUTE_LOGGER]
        assert [(record.levelname, record.exc_info, record.exc_text) for record in ours] == [("WARNING", None, None)]
        assert CALLER_CHOSEN_ALG not in caplog.text

    def test_an_error_the_library_does_not_type_still_refuses_the_token(self, monkeypatch, deployment, caplog):
        """PyJWT's ``DecodeError`` for a header that is not JSON (identity-model#1179)."""
        token = deployment.ops[DESCOPE].token(RS256, deployment.id_token_claims(DESCOPE))
        _header, payload, signature = token.split(".")

        with caplog.at_level("DEBUG"):
            _assert_uniform_401(_post(_deployed(monkeypatch, deployment), f"bm90LWpzb24.{payload}.{signature}"))

        assert _route_records(caplog) == [("WARNING", "ID validation refused: DecodeError")]


class TestProviderFault:
    """A provider that cannot be read is not a bad token: 503, logged as an error.

    The library retries a failed fetch before it gives up, so the tests of an
    unreachable or failing endpoint take a few seconds each.

    Each test has a provider of its own. The library caches discovery and the
    JWKS per address, so a provider another test already read would be answered
    from the cache and its fault never seen.
    """

    @pytest.fixture
    def faulty(self) -> LocalProvider:
        with local_provider() as provider:
            yield provider

    def _assert_503(self, response) -> None:
        assert response.status_code == 503, response.text
        assert response.json() == PROVIDER_UNAVAILABLE

    def _token(self, op: LocalProvider) -> str:
        return op.token(RS256, _recorded(KEYCLOAK_FIXTURES, "id-token.json", op))

    def test_control_the_same_token_validates_while_the_provider_answers(self, monkeypatch, faulty):
        response = _post(_client(monkeypatch, [_keycloak(faulty)]), self._token(faulty))

        assert response.status_code == 200, response.text

    def test_a_discovery_address_that_refuses_the_connection(self, monkeypatch, faulty, caplog):
        provider = replace(_keycloak(faulty), disco_address=f"http://127.0.0.1:{_closed_port()}{faulty.discovery_path}")

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, [provider]), self._token(faulty)))

        assert _route_records(caplog) == [
            ("ERROR", "ID validation hit a provider fault: DiscoveryException (status None)")
        ]

    def test_a_discovery_endpoint_that_answers_500(self, monkeypatch, faulty, caplog):
        faulty.fail(faulty.discovery_path, 500)

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, [_keycloak(faulty)]), self._token(faulty)))

        assert _route_records(caplog) == [
            ("ERROR", "ID validation hit a provider fault: DiscoveryException (status 500)")
        ]

    def test_a_jwks_endpoint_that_answers_500(self, monkeypatch, faulty, caplog):
        faulty.fail(faulty.jwks_path, 500)

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, [_keycloak(faulty)]), self._token(faulty)))

        assert _route_records(caplog) == [("ERROR", "ID validation hit a provider fault: JwksException (status 500)")]

    def test_a_discovery_document_for_another_issuer(self, monkeypatch, faulty, caplog):
        faulty.publish(issuer=f"http://127.0.0.1:{_closed_port()}")

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, [_keycloak(faulty)]), self._token(faulty)))

        assert _route_records(caplog) == [("ERROR", "ID validation hit a provider fault: DiscoveryIssuerMismatchError")]

    def test_a_jwks_with_no_keys(self, monkeypatch, faulty, caplog):
        faulty.publish(keys=[])

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, [_keycloak(faulty)]), self._token(faulty)))

        assert _route_records(caplog) == [("ERROR", "ID validation hit a provider fault: JwksValidationError")]

    def test_a_plain_http_provider_where_https_is_required(self, monkeypatch, faulty, caplog):
        """Refused by the library's discovery policy before any request is sent."""
        provider = replace(
            _keycloak(faulty), disco_address=f"http://provider.invalid{faulty.discovery_path}", require_https=True
        )

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, [provider]), self._token(faulty)))

        assert _route_records(caplog) == [("ERROR", "ID validation hit a provider fault: DiscoveryHTTPSRequiredError")]

    def test_ory_with_an_audience_and_no_client_id(self, monkeypatch, faulty, caplog):
        """``ORY_AUDIENCE`` is the API's: an ID token is not checked against it, so a Hydra
        access token for the API is not taken for an ID token."""
        provider = ory_config(faulty.issuer, ORY_AUDIENCE)
        token = faulty.token(RS256, _hydra_access_token_claims(faulty, [ORY_AUDIENCE]))

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, [provider]), token))

        assert id_token_client_id(provider) is None
        assert _route_records(caplog) == [("ERROR", "ID validation hit a provider fault: NO_CLIENT_ID (Ory)")]

    def test_a_provider_with_no_client_id(self, monkeypatch, faulty, caplog):
        """Ory with audience enforcement knowingly off and no ``ORY_CLIENT_ID``: no audience to check."""
        provider = ory_config(faulty.issuer, None, require_audience=False)
        token = faulty.token(RS256, {"iss": faulty.issuer, "sub": "ory-user", "aud": "anyone"})

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, [provider]), token))

        assert _route_records(caplog) == [("ERROR", "ID validation hit a provider fault: NO_CLIENT_ID (Ory)")]

    def test_a_fault_is_logged_without_the_token_or_a_traceback(self, monkeypatch, faulty, caplog):
        faulty.publish(keys=[])
        token = faulty.token_with_header_alg(CALLER_CHOSEN_ALG, _recorded(KEYCLOAK_FIXTURES, "id-token.json", faulty))

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, [_keycloak(faulty)]), token))

        ours = [record for record in caplog.records if record.name == ROUTE_LOGGER]
        assert [(record.levelname, record.exc_info, record.exc_text) for record in ours] == [("ERROR", None, None)]
        assert token not in caplog.text
        assert CALLER_CHOSEN_ALG not in caplog.text


def test_a_header_that_is_not_a_bearer_token_is_still_a_400(monkeypatch, deployment):
    response = _deployed(monkeypatch, deployment).post(ROUTE, headers={"Authorization": "Basic abc"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid authorization header"}
