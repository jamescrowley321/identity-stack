"""Unit tests for the attributes router (user profile + tenant settings)."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


def _make_http_status_error(status_code: int = 500) -> httpx.HTTPStatusError:
    """Create a mock HTTPStatusError for testing Descope API failures."""
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/test")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"{status_code} Server Error", request=request, response=response)


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


ADMIN_CLAIMS = {
    "sub": "user123",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["admin"], "permissions": ["settings.manage"]},
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["viewer"], "permissions": ["projects.read"]},
    },
}

NO_TENANT_CLAIMS = {
    "sub": "user789",
    "tenants": {},
}


@pytest.mark.anyio
async def test_profile_rejects_unauthenticated(client):
    response = await client.get("/api/profile")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.routers.attributes.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_user.return_value = {
        "name": "Test User",
        "email": "test@example.com",
        "customAttributes": {"department": "Engineering", "job_title": "Developer"},
    }
    mock_factory.return_value = mock_client

    response = await client.get("/api/profile", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == "user123"
    assert data["custom_attributes"]["department"] == "Engineering"


@pytest.mark.anyio
@patch("app.routers.attributes.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_handles_api_failure(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_user.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.get("/api/profile", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 502
    assert "identity provider" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.attributes.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_attribute(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.patch(
        "/api/profile",
        headers={"Authorization": "Bearer valid.token"},
        json={"key": "department", "value": "Product"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    mock_client.update_user_custom_attribute.assert_called_once_with("user123", "department", "Product")


@pytest.mark.anyio
@patch("app.routers.attributes.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_tenant_settings(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_tenant.return_value = {
        "name": "Acme Corp",
        "customAttributes": {"plan_tier": "pro", "max_members": 50},
    }
    mock_factory.return_value = mock_client

    response = await client.get("/api/tenants/current/settings", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == "tenant-abc"
    assert data["custom_attributes"]["plan_tier"] == "pro"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_tenant_settings_403_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.get("/api/tenants/current/settings", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.routers.attributes.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_as_admin(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.patch(
        "/api/tenants/current/settings",
        headers={"Authorization": "Bearer valid.token"},
        json={"custom_attributes": {"plan_tier": "enterprise", "max_members": 200}},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    mock_client.update_tenant_custom_attributes.assert_called_once_with(
        "tenant-abc", {"plan_tier": "enterprise", "max_members": 200}
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.patch(
        "/api/tenants/current/settings",
        headers={"Authorization": "Bearer valid.token"},
        json={"custom_attributes": {"plan_tier": "enterprise"}},
    )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_update_profile_rejects_unauthenticated(client):
    response = await client.patch("/api/profile", json={"key": "department", "value": "Eng"})
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.routers.attributes.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_tenant_settings_handles_api_failure(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_tenant.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.get("/api/tenants/current/settings", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 502
    assert "identity provider" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_rejects_disallowed_key(mock_validate, client):
    """Should reject attribute keys not in the allowlist."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.patch(
        "/api/profile",
        headers={"Authorization": "Bearer valid.token"},
        json={"key": "admin_override", "value": "true"},
    )
    assert response.status_code == 400
    assert "not allowed" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_rejects_missing_sub(mock_validate, client):
    """Should return 400 if the JWT has no sub claim."""
    mock_validate.return_value = {"email": "test@example.com", "dct": "tenant-abc", "tenants": {}}
    response = await client.patch(
        "/api/profile",
        headers={"Authorization": "Bearer valid.token"},
        json={"key": "department", "value": "Eng"},
    )
    assert response.status_code == 400
    assert "sub" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.attributes.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_handles_api_failure(mock_validate, mock_factory, client):
    """M3: update_profile_attribute should return 502 on Descope API error."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.update_user_custom_attribute.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.patch(
        "/api/profile",
        headers={"Authorization": "Bearer valid.token"},
        json={"key": "department", "value": "Eng"},
    )
    assert response.status_code == 502
    assert "identity provider" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_rejects_disallowed_keys(mock_validate, client):
    """M1: tenant settings should reject attribute keys not in the allowlist."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.patch(
        "/api/tenants/current/settings",
        headers={"Authorization": "Bearer valid.token"},
        json={"custom_attributes": {"evil_key": "pwned"}},
    )
    assert response.status_code == 400
    assert "not allowed" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_rejects_missing_sub(mock_validate, client):
    """S1: get_profile should return 401 when sub claim is missing."""
    mock_validate.return_value = {"email": "test@example.com", "dct": "tenant-abc", "tenants": {}}
    response = await client.get("/api/profile", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 401
    assert "sub" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.attributes.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_handles_api_failure(mock_validate, mock_factory, client):
    """Tenant settings update should return 502 on Descope API error."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.update_tenant_custom_attributes.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.patch(
        "/api/tenants/current/settings",
        headers={"Authorization": "Bearer valid.token"},
        json={"custom_attributes": {"plan_tier": "pro"}},
    )
    assert response.status_code == 502
    assert "identity provider" in response.json()["detail"]
