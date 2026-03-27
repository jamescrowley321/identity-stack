"""Unit tests for the auth router (logout and auth method endpoints)."""

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


# --- /api/auth/method tests ---

OAUTH_GOOGLE_CLAIMS = {
    "sub": "user-google",
    "amr": ["google"],
}

OAUTH_GITHUB_CLAIMS = {
    "sub": "user-github",
    "amr": ["github"],
}

PASSWORD_CLAIMS = {
    "sub": "user-pwd",
    "amr": ["pwd"],
}

OTP_CLAIMS = {
    "sub": "user-otp",
    "amr": ["otp"],
}

NO_AMR_CLAIMS = {
    "sub": "user-noamr",
}

WEBAUTHN_CLAIMS = {
    "sub": "user-passkey",
    "amr": ["webauthn"],
}

MULTI_AMR_CLAIMS = {
    "sub": "user-multi",
    "amr": ["pwd", "mfa"],
}


@pytest.mark.anyio
async def test_auth_method_rejects_unauthenticated(client):
    response = await client.get("/api/auth/method")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_auth_method_google(mock_validate, client):
    mock_validate.return_value = OAUTH_GOOGLE_CLAIMS
    response = await client.get("/api/auth/method", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "oauth"
    assert data["provider"] == "google"
    assert data["amr"] == ["google"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_auth_method_github(mock_validate, client):
    mock_validate.return_value = OAUTH_GITHUB_CLAIMS
    response = await client.get("/api/auth/method", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "oauth"
    assert data["provider"] == "github"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_auth_method_password(mock_validate, client):
    mock_validate.return_value = PASSWORD_CLAIMS
    response = await client.get("/api/auth/method", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "password"
    assert data["provider"] is None


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_auth_method_otp(mock_validate, client):
    mock_validate.return_value = OTP_CLAIMS
    response = await client.get("/api/auth/method", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    assert response.json()["method"] == "otp"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_auth_method_no_amr(mock_validate, client):
    mock_validate.return_value = NO_AMR_CLAIMS
    response = await client.get("/api/auth/method", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "unknown"
    assert data["provider"] is None
    assert data["amr"] == []


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_auth_method_mfa(mock_validate, client):
    mock_validate.return_value = MULTI_AMR_CLAIMS
    response = await client.get("/api/auth/method", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "password"
    assert data["amr"] == ["pwd", "mfa"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_auth_method_passkey(mock_validate, client):
    mock_validate.return_value = WEBAUTHN_CLAIMS
    response = await client.get("/api/auth/method", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "passkey"
    assert data["provider"] is None
    assert data["amr"] == ["webauthn"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_auth_method_invalid_amr_type(mock_validate, client):
    """amr claim that is not a list should be treated as empty."""
    mock_validate.return_value = {"sub": "u1", "amr": "not-a-list"}
    response = await client.get("/api/auth/method", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "unknown"
    assert data["amr"] == []
