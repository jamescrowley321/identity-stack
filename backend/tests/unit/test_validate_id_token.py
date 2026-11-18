"""``POST /api/validate-id-token``: the Descope provider's rules, with no middleware in front.

The route is excluded from both token middlewares, so it applies the Descope
provider's signing-algorithm set itself and answers a refused token with the
same 401 body as they do.

Nothing in the library is patched: a loopback provider
(``tests/helpers/local_op.py``) serves discovery and a JWKS with an RS256, an
ES256 and a PS256 key, and the route validates tokens signed by those keys. Only
the provider's discovery address is pointed at the loopback provider.
"""

import socket
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.middleware.providers import descope_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.routers import protected
from tests.helpers.local_op import ES256, HS256, PS256, RS256, UNSIGNED, LocalProvider, local_provider

ROUTE = "/api/validate-id-token"
ROUTE_LOGGER = "app.routers.protected"
UNIFORM_401 = {"detail": "Invalid or expired token"}
PROVIDER_UNAVAILABLE = {"detail": "Identity provider unavailable"}
PROJECT_ID = "P2validateidtokencheck"
# The JWK thumbprint of the example proof key in RFC 9449 §6.1.
DPOP_KEY_THUMBPRINT = "0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I"
# The certificate thumbprint in the RFC 8705 §3.1 example token.
CERTIFICATE_THUMBPRINT = "bwcK0esc3ACC3DB2Y5_lESsXE8o9ltc05O89jdN-dg2"
CALLER_CHOSEN_ALG = "CALLER-CHOSEN-ALG-7f3a"


@pytest.fixture(scope="module")
def op() -> LocalProvider:
    with local_provider() as provider:
        yield provider


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


def _client(monkeypatch, op: LocalProvider, **changes) -> TestClient:
    """The route alone, on the shipped Descope config fetching from the loopback provider."""
    provider = replace(descope_config(PROJECT_ID), **{"disco_address": op.disco_address, **changes})
    monkeypatch.setattr(protected, "DESCOPE", provider)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(protected.router, prefix="/api")
    return TestClient(app)


def _claims(op: LocalProvider) -> dict:
    return {"iss": op.issuer, "sub": "descope-user", "aud": PROJECT_ID}


def _post(client: TestClient, token: str):
    return client.post(ROUTE, headers={"Authorization": f"Bearer {token}"})


def _assert_uniform_401(response) -> None:
    assert response.status_code == 401, response.text
    assert response.json() == UNIFORM_401


def test_the_route_validates_with_the_descope_provider_config():
    assert protected.DESCOPE == descope_config(protected.DESCOPE_PROJECT_ID)


def test_an_rs256_token_is_validated_and_its_claims_returned(monkeypatch, op):
    response = _post(_client(monkeypatch, op), op.token(RS256, _claims(op)))

    assert response.status_code == 200, response.text
    returned = response.json()
    assert {name: returned[name] for name in ("iss", "sub", "aud")} == _claims(op)


@pytest.mark.parametrize("alg", [ES256, PS256])
def test_an_algorithm_outside_the_descope_set_is_refused(monkeypatch, op, alg):
    """Signed by a key in the same JWKS, refused by the shipped config."""
    token = op.token(alg, _claims(op))

    _assert_uniform_401(_post(_client(monkeypatch, op), token))

    # Control: the algorithm set is the only reason. Without it the same token validates.
    unpinned = _post(_client(monkeypatch, op, algorithms=frozenset()), token)
    assert unpinned.status_code == 200, unpinned.text


@pytest.mark.parametrize("alg", [HS256, UNSIGNED])
def test_symmetric_and_unsigned_tokens_are_refused(monkeypatch, op, alg):
    _assert_uniform_401(_post(_client(monkeypatch, op), op.token(alg, _claims(op))))


@pytest.mark.parametrize(
    "token",
    ["not-a-jwt", "a.b.c", ""],
    ids=["opaque", "three-segments-of-nothing", "empty"],
)
def test_a_token_that_is_not_a_jwt_is_refused(monkeypatch, op, token):
    """The library raises PyJWT's error for these, not its own (identity-model#1179)."""
    _assert_uniform_401(_post(_client(monkeypatch, op), token))


def test_a_token_for_another_audience_is_refused(monkeypatch, op):
    token = op.token(RS256, {**_claims(op), "aud": "another-project"})

    _assert_uniform_401(_post(_client(monkeypatch, op), token))


def test_an_expired_token_is_refused(monkeypatch, op):
    token = op.token(RS256, {**_claims(op), "exp": 1})

    _assert_uniform_401(_post(_client(monkeypatch, op), token))


def test_a_dpop_bound_token_sent_as_a_bearer_token_is_refused(monkeypatch, op):
    token = op.token(RS256, {**_claims(op), "cnf": {"jkt": DPOP_KEY_THUMBPRINT}})

    _assert_uniform_401(_post(_client(monkeypatch, op), token))


def _route_records(caplog) -> list[tuple[str, str]]:
    """Level and message of each record the route's own logger wrote."""
    return [(record.levelname, record.getMessage()) for record in caplog.records if record.name == ROUTE_LOGGER]


def test_a_refusal_is_one_log_line_naming_the_error_type(monkeypatch, op, caplog):
    token = op.token(ES256, _claims(op))

    with caplog.at_level("DEBUG"):
        _assert_uniform_401(_post(_client(monkeypatch, op), token))

    assert _route_records(caplog) == [("WARNING", "ID validation refused: TokenValidationException")]
    assert token not in caplog.text


def test_a_refusal_with_a_typed_kind_logs_the_kind(monkeypatch, op, caplog):
    token = op.token(RS256, {**_claims(op), "cnf": {"x5t#S256": CERTIFICATE_THUMBPRINT}})

    with caplog.at_level("DEBUG"):
        _assert_uniform_401(_post(_client(monkeypatch, op), token))

    assert _route_records(caplog) == [
        ("WARNING", "ID validation refused: CertificateBindingError (certificate_required)")
    ]


def test_a_refusal_logs_no_traceback_and_nothing_the_caller_chose(monkeypatch, op, caplog):
    """The header ``alg`` is the caller's text, and the library quotes it in its error."""
    token = op.token_with_header_alg(CALLER_CHOSEN_ALG, _claims(op))

    with caplog.at_level("DEBUG"):
        _assert_uniform_401(_post(_client(monkeypatch, op), token))

    ours = [record for record in caplog.records if record.name == ROUTE_LOGGER]
    assert [(record.levelname, record.exc_info, record.exc_text) for record in ours] == [("WARNING", None, None)]
    assert CALLER_CHOSEN_ALG not in caplog.text


class TestProviderFault:
    """A provider that cannot be read is not a bad token: 503, logged as an error.

    The library retries a failed fetch before it gives up, so the three tests
    of an unreachable or failing endpoint take a few seconds each.

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

    def test_control_the_same_token_validates_while_the_provider_answers(self, monkeypatch, faulty):
        response = _post(_client(monkeypatch, faulty), faulty.token(RS256, _claims(faulty)))

        assert response.status_code == 200, response.text

    def test_a_discovery_address_that_refuses_the_connection(self, monkeypatch, faulty, caplog):
        token = faulty.token(RS256, _claims(faulty))
        client = _client(monkeypatch, faulty, disco_address=f"http://127.0.0.1:{_closed_port()}{faulty.discovery_path}")

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(client, token))

        assert _route_records(caplog) == [
            ("ERROR", "ID validation hit a provider fault: DiscoveryException (status None)")
        ]

    def test_a_discovery_endpoint_that_answers_500(self, monkeypatch, faulty, caplog):
        faulty.fail(faulty.discovery_path, 500)

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, faulty), faulty.token(RS256, _claims(faulty))))

        assert _route_records(caplog) == [
            ("ERROR", "ID validation hit a provider fault: DiscoveryException (status 500)")
        ]

    def test_a_jwks_endpoint_that_answers_500(self, monkeypatch, faulty, caplog):
        faulty.fail(faulty.jwks_path, 500)

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, faulty), faulty.token(RS256, _claims(faulty))))

        assert _route_records(caplog) == [("ERROR", "ID validation hit a provider fault: JwksException (status 500)")]

    def test_a_discovery_document_for_another_issuer(self, monkeypatch, faulty, caplog):
        faulty.publish(issuer=f"http://127.0.0.1:{_closed_port()}")

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, faulty), faulty.token(RS256, _claims(faulty))))

        assert _route_records(caplog) == [("ERROR", "ID validation hit a provider fault: DiscoveryIssuerMismatchError")]

    def test_a_jwks_with_no_keys(self, monkeypatch, faulty, caplog):
        faulty.publish(keys=[])

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, faulty), faulty.token(RS256, _claims(faulty))))

        assert _route_records(caplog) == [("ERROR", "ID validation hit a provider fault: JwksValidationError")]

    def test_a_plain_http_provider_where_https_is_required(self, monkeypatch, faulty, caplog):
        """Refused by the library's discovery policy before any request is sent."""
        client = _client(
            monkeypatch,
            faulty,
            disco_address=f"http://provider.invalid{faulty.discovery_path}",
            require_https=True,
        )

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(client, faulty.token(RS256, _claims(faulty))))

        assert _route_records(caplog) == [("ERROR", "ID validation hit a provider fault: DiscoveryHTTPSRequiredError")]

    def test_a_fault_is_logged_without_the_token_or_a_traceback(self, monkeypatch, faulty, caplog):
        faulty.publish(keys=[])
        token = faulty.token_with_header_alg(CALLER_CHOSEN_ALG, _claims(faulty))

        with caplog.at_level("DEBUG"):
            self._assert_503(_post(_client(monkeypatch, faulty), token))

        ours = [record for record in caplog.records if record.name == ROUTE_LOGGER]
        assert [(record.levelname, record.exc_info, record.exc_text) for record in ours] == [("ERROR", None, None)]
        assert token not in caplog.text
        assert CALLER_CHOSEN_ALG not in caplog.text


def test_an_error_the_library_does_not_type_still_refuses_the_token(monkeypatch, op, caplog):
    """PyJWT's ``DecodeError`` for a token that is not a JWT (identity-model#1179)."""
    with caplog.at_level("DEBUG"):
        _assert_uniform_401(_post(_client(monkeypatch, op), "not-a-jwt"))

    assert _route_records(caplog) == [("WARNING", "ID validation refused: DecodeError")]


def test_a_header_that_is_not_a_bearer_token_is_still_a_400(monkeypatch, op):
    response = _client(monkeypatch, op).post(ROUTE, headers={"Authorization": "Basic abc"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid authorization header"}
