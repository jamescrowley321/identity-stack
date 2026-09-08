"""Unit tests for the token validation middleware."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.routing import Route

from app.main import app
from app.middleware.auth import TokenValidationMiddleware

# Build a mock issuer matching the Descope format the middleware accepts.
# When DESCOPE_PROJECT_ID is set (CI), the issuer must match; when unset,
# the middleware skips issuer validation so any value works.
_mock_project_id = os.getenv("DESCOPE_PROJECT_ID", "test-project-id")
_mock_issuer = f"https://api.descope.com/{_mock_project_id}"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_protected_route_rejects_missing_auth(client):
    """Protected endpoints should return 401 without Authorization header."""
    response = await client.get("/api/me")
    assert response.status_code == 401
    assert "Missing" in response.json()["detail"]


@pytest.mark.anyio
async def test_protected_route_rejects_invalid_scheme(client):
    """Protected endpoints should reject non-Bearer auth schemes."""
    response = await client.get("/api/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401


@pytest.mark.anyio
async def test_protected_route_rejects_invalid_token(client):
    """Protected endpoints should return 401 for invalid JWT."""
    response = await client.get("/api/me", headers={"Authorization": "Bearer invalid.token.here"})
    assert response.status_code == 401
    assert "Invalid" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_protected_route_accepts_valid_token(mock_validate, client):
    """Protected endpoints should pass through with a valid token."""
    mock_claims = {
        "sub": "user123",
        "email": "test@example.com",
        "name": "Test User",
        "iss": _mock_issuer,
    }
    mock_validate.return_value = mock_claims

    response = await client.get("/api/me", headers={"Authorization": "Bearer valid.mock.token"})
    assert response.status_code == 200
    data = response.json()
    identity = data["identity"]
    assert identity["is_authenticated"] is True
    assert identity["authentication_type"] == "Descope"
    claim_types = {c["type"]: c for c in identity["claims"]}
    assert claim_types["sub"]["value"] == "user123"
    assert claim_types["email"]["value"] == "test@example.com"
    assert claim_types["sub"]["issuer"] == _mock_issuer


@pytest.mark.anyio
async def test_excluded_path_skips_auth(client):
    """Excluded paths should not require authentication."""
    response = await client.get("/api/health")
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_middleware_sets_tenant_id_from_dct_claim(mock_validate, client):
    """Middleware should extract the dct claim and set request.state.tenant_id."""
    mock_claims = {
        "sub": "user123",
        "dct": "tenant-abc",
        "iss": _mock_issuer,
    }
    mock_validate.return_value = mock_claims

    # /api/claims returns the raw claims — if middleware set tenant_id correctly,
    # the tenant-scoped endpoints that depend on it will work.
    response = await client.get("/api/claims", headers={"Authorization": "Bearer valid.mock.token"})
    assert response.status_code == 200
    data = response.json()
    assert data["dct"] == "tenant-abc"


def _app_with_failing_route(exc: Exception):
    """A minimal app behind TokenValidationMiddleware whose only route raises."""
    inner = Starlette(
        routes=[Route("/boom", lambda request: (_ for _ in ()).throw(exc))],
    )
    inner.add_middleware(TokenValidationMiddleware, descope_project_id=_mock_project_id)
    return inner


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_downstream_exception_is_not_reported_as_invalid_token(mock_validate):
    """A handler that raises must surface as a server error, never as a 401.

    Regression: ``call_next`` used to be inside the authentication try/except, so
    every unhandled downstream exception came back as
    ``401 {"detail": "Invalid or expired token"}``. In CI the E2E backend ran on a
    schema-less database, and the resulting ``no such table`` errors were reported
    as 62 authentication failures — the real fault was invisible.
    """
    mock_validate.return_value = {"sub": "user123", "iss": _mock_issuer}

    transport = ASGITransport(app=_app_with_failing_route(RuntimeError("boom")), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/boom", headers={"Authorization": "Bearer valid.mock.token"})

    assert response.status_code == 500, (
        f"downstream failure reported as {response.status_code} — "
        "authentication must not swallow request-handling errors"
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_rejected_token_never_reaches_the_route(mock_validate):
    """The 401 path still holds: an unvalidatable token stops before the handler."""
    mock_validate.side_effect = ValueError("bad signature")

    transport = ASGITransport(
        app=_app_with_failing_route(AssertionError("route must not run")),
        raise_app_exceptions=False,
    )
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/boom", headers={"Authorization": "Bearer valid.mock.token"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired token"
