"""Unit tests for the attributes router (user profile + tenant settings)."""

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
    mock_client.load_user.side_effect = Exception("API down")
    mock_factory.return_value = mock_client

    response = await client.get("/api/profile", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    data = response.json()
    assert data["custom_attributes"] == {}


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
