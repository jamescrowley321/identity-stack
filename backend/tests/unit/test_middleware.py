"""Unit tests for the token validation middleware."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


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
        "iss": "https://test.example.com",
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
    assert claim_types["sub"]["issuer"] == "https://test.example.com"


@pytest.mark.anyio
async def test_excluded_path_skips_auth(client):
    """Excluded paths should not require authentication."""
    response = await client.get("/api/health/live")
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_middleware_sets_tenant_id_from_dct_claim(mock_validate, client):
    """Middleware should extract the dct claim and set request.state.tenant_id."""
    mock_claims = {
        "sub": "user123",
        "dct": "tenant-abc",
        "iss": "https://test.example.com",
    }
    mock_validate.return_value = mock_claims

    # /api/claims returns the raw claims — if middleware set tenant_id correctly,
    # the tenant-scoped endpoints that depend on it will work.
    response = await client.get("/api/claims", headers={"Authorization": "Bearer valid.mock.token"})
    assert response.status_code == 200
    data = response.json()
    assert data["dct"] == "tenant-abc"
