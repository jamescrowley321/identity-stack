"""Unit tests for the auth router (logout endpoint)."""

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
async def test_logout_rejects_missing_auth(client):
    """Logout should return 401 without Authorization header."""
    response = await client.post("/api/auth/logout")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_logout_rejects_invalid_token(client):
    """Logout should return 401 for an invalid JWT."""
    response = await client.post(
        "/api/auth/logout",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_logout_succeeds_with_valid_token(mock_validate, client):
    """Logout should return 200 with the user's sub claim."""
    mock_claims = {
        "sub": "user123",
        "email": "test@example.com",
        "name": "Test User",
        "iss": "https://test.example.com",
    }
    mock_validate.return_value = mock_claims

    response = await client.post(
        "/api/auth/logout",
        headers={"Authorization": "Bearer valid.mock.token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "logged_out"
    assert data["sub"] == "user123"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_logout_returns_null_sub_when_missing(mock_validate, client):
    """Logout should handle tokens without a sub claim."""
    mock_claims = {
        "email": "test@example.com",
        "iss": "https://test.example.com",
    }
    mock_validate.return_value = mock_claims

    response = await client.post(
        "/api/auth/logout",
        headers={"Authorization": "Bearer valid.mock.token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "logged_out"
    assert data["sub"] is None
