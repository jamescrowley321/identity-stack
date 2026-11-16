"""``POST /api/validate-id-token``: the Descope provider's rules, with no middleware in front.

The route is excluded from both token middlewares, so it applies the Descope
provider's signing-algorithm set itself and answers a refused token with the
same 401 body as they do.

Nothing in the library is patched: a loopback provider
(``tests/helpers/local_op.py``) serves discovery and a JWKS with an RS256, an
ES256 and a PS256 key, and the route validates tokens signed by those keys. Only
the provider's discovery address is pointed at the loopback provider.
"""

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from py_identity_model import TokenValidationException
from slowapi.errors import RateLimitExceeded

from app.middleware.providers import descope_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.routers import protected
from tests.helpers.local_op import ES256, HS256, PS256, RS256, UNSIGNED, LocalProvider, local_provider

ROUTE = "/api/validate-id-token"
UNIFORM_401 = {"detail": "Invalid or expired token"}
PROJECT_ID = "P2validateidtokencheck"
# The JWK thumbprint of the example proof key in RFC 9449 §6.1.
DPOP_KEY_THUMBPRINT = "0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I"


@pytest.fixture(scope="module")
def op() -> LocalProvider:
    with local_provider() as provider:
        yield provider


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    limiter.reset()
    yield
    limiter.reset()


def _client(monkeypatch, op: LocalProvider, **changes) -> TestClient:
    """The route alone, on the shipped Descope config fetching from the loopback provider."""
    provider = replace(descope_config(PROJECT_ID), disco_address=op.disco_address, **changes)
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


def test_the_refusal_is_logged_with_its_cause_and_without_the_token(monkeypatch, op, caplog):
    token = op.token(ES256, _claims(op))

    with caplog.at_level("WARNING", logger="app.routers.protected"):
        _assert_uniform_401(_post(_client(monkeypatch, op), token))

    causes = {record.getMessage(): record.exc_info[1] for record in caplog.records}
    assert list(causes) == ["validate-id-token refused the token"]
    cause = causes["validate-id-token refused the token"]
    assert isinstance(cause, TokenValidationException)
    assert str(cause) == "Algorithm 'ES256' is not in the allowed algorithms"
    assert token not in caplog.text


def test_a_header_that_is_not_a_bearer_token_is_still_a_400(monkeypatch, op):
    response = _client(monkeypatch, op).post(ROUTE, headers={"Authorization": "Basic abc"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid authorization header"}
