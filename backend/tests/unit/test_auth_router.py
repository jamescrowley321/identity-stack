"""Unit tests for the auth router (logout endpoint)."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


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
@patch("app.routers.auth.httpx.AsyncClient")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_logout_succeeds_with_valid_token(mock_validate, mock_httpx_cls, client):
    """Logout should return 200, call Descope logout API, and return the user's sub."""
    mock_claims = {
        "sub": "user123",
        "email": "test@example.com",
        "name": "Test User",
        "iss": "https://test.example.com",
    }
    mock_validate.return_value = mock_claims

    mock_client = AsyncMock()
    mock_httpx_cls.return_value.__aenter__.return_value = mock_client

    response = await client.post(
        "/api/auth/logout",
        headers={"Authorization": "Bearer valid.mock.token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "logged_out"
    assert data["sub"] == "user123"

    mock_client.post.assert_called_once_with(
        "https://api.descope.com/v1/mgmt/user/logout",
        headers={"Authorization": "Bearer test-project-id:test-management-key"},
        json={"userId": "user123"},
    )


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


@pytest.mark.anyio
@patch("app.routers.auth.httpx.AsyncClient")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_logout_skips_api_call_without_management_key(mock_validate, mock_httpx_cls, client, monkeypatch):
    """Logout should skip the Descope API call when management key is not set."""
    monkeypatch.delenv("DESCOPE_MANAGEMENT_KEY")
    mock_claims = {"sub": "user123"}
    mock_validate.return_value = mock_claims

    response = await client.post(
        "/api/auth/logout",
        headers={"Authorization": "Bearer valid.mock.token"},
    )
    assert response.status_code == 200
    mock_httpx_cls.return_value.__aenter__.return_value.post.assert_not_called()
