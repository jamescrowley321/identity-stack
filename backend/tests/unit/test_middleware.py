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
@patch("app.middleware.auth.to_principal")
async def test_protected_route_accepts_valid_token(mock_to_principal, mock_validate, client):
    """Protected endpoints should pass through with a valid token."""
    mock_claims = {"sub": "user123", "email": "test@example.com", "name": "Test User"}
    mock_validate.return_value = mock_claims

    def _find_first(self, key):
        if key in mock_claims:
            return type("Claim", (), {"value": mock_claims.get(key)})()
        return None

    mock_principal = type("FakePrincipal", (), {"find_first": _find_first})()
    mock_to_principal.return_value = mock_principal

    response = await client.get("/api/me", headers={"Authorization": "Bearer valid.mock.token"})
    assert response.status_code == 200
    data = response.json()
    assert data["sub"] == "user123"
    assert data["email"] == "test@example.com"


@pytest.mark.anyio
async def test_excluded_path_skips_auth(client):
    """Excluded paths should not require authentication."""
    response = await client.get("/api/health")
    assert response.status_code == 200
