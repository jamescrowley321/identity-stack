"""Unit tests for the access keys router."""

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

KEY_IN_TENANT = {"id": "key123", "name": "Test", "status": "active", "keyTenants": [{"tenantId": "tenant-abc"}]}
KEY_OTHER_TENANT = {"id": "key999", "name": "Other", "status": "active", "keyTenants": [{"tenantId": "tenant-other"}]}


@pytest.mark.anyio
async def test_list_keys_rejects_unauthenticated(client):
    response = await client.get("/api/keys")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_keys_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get("/api/keys", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.routers.accesskeys.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_keys_as_admin(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.search_access_keys.return_value = [KEY_IN_TENANT]
    mock_factory.return_value = mock_client

    response = await client.get("/api/keys", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    assert len(response.json()["keys"]) == 1
    mock_client.search_access_keys.assert_called_once_with("tenant-abc")


@pytest.mark.anyio
@patch("app.routers.accesskeys.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_key(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_access_key.return_value = {
        "key": {"id": "new-key-id", "name": "My API Key"},
        "cleartext": "secret-key-value-shown-once",
    }
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/keys",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "My API Key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["cleartext"] == "secret-key-value-shown-once"


@pytest.mark.anyio
@patch("app.routers.accesskeys.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_key_with_options(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_access_key.return_value = {"key": {"id": "k1"}, "cleartext": "secret"}
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/keys",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "Scoped Key", "expire_time": 1700000000, "role_names": ["viewer"]},
    )
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_key_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/keys",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "Sneaky Key"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.routers.accesskeys.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_key(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_access_key.return_value = KEY_IN_TENANT
    mock_factory.return_value = mock_client

    response = await client.get("/api/keys/key123", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    assert response.json()["name"] == "Test"


@pytest.mark.anyio
@patch("app.routers.accesskeys.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_key_cross_tenant_rejected(mock_validate, mock_factory, client):
    """Loading a key from another tenant should be rejected."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_access_key.return_value = KEY_OTHER_TENANT
    mock_factory.return_value = mock_client

    response = await client.get("/api/keys/key999", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.routers.accesskeys.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_key(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_access_key.return_value = KEY_IN_TENANT
    mock_factory.return_value = mock_client

    response = await client.post("/api/keys/key123/deactivate", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    assert response.json()["status"] == "deactivated"
    mock_client.deactivate_access_key.assert_called_once_with("key123")


@pytest.mark.anyio
@patch("app.routers.accesskeys.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_key_cross_tenant_rejected(mock_validate, mock_factory, client):
    """Deactivating a key from another tenant should be rejected."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_access_key.return_value = KEY_OTHER_TENANT
    mock_factory.return_value = mock_client

    response = await client.post("/api/keys/key999/deactivate", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.routers.accesskeys.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_activate_key(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_access_key.return_value = KEY_IN_TENANT
    mock_factory.return_value = mock_client

    response = await client.post("/api/keys/key123/activate", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    assert response.json()["status"] == "activated"


@pytest.mark.anyio
@patch("app.routers.accesskeys.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_key(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_access_key.return_value = KEY_IN_TENANT
    mock_factory.return_value = mock_client

    response = await client.delete("/api/keys/key123", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    mock_client.delete_access_key.assert_called_once_with("key123")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_key_rejected_without_tenant(mock_validate, client):
    mock_validate.return_value = {"sub": "user789", "tenants": {}}
    response = await client.post(
        "/api/keys",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "No Tenant Key"},
    )
    assert response.status_code == 403
