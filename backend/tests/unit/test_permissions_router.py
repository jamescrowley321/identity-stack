"""Unit tests for the permissions router."""

from unittest.mock import AsyncMock, patch

import httpx
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

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

SAMPLE_PERMISSIONS = [
    {"name": "reports.read", "description": "View reports"},
    {"name": "reports.write", "description": "Edit reports"},
]


# --- Auth enforcement ---


@pytest.mark.anyio
async def test_list_permissions_rejects_unauthenticated(client):
    response = await client.get("/api/permissions")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_permissions_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get("/api/permissions", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post("/api/permissions", headers=AUTH_HEADER, json={"name": "test.perm"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_permission_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.put("/api/permissions/test.perm", headers=AUTH_HEADER, json={"new_name": "test.perm2"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_permission_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.delete("/api/permissions/test.perm", headers=AUTH_HEADER)
    assert response.status_code == 403


# --- Happy path ---


@pytest.mark.anyio
@patch("app.routers.permissions.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_permissions(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_permissions.return_value = SAMPLE_PERMISSIONS
    mock_factory.return_value = mock_client

    response = await client.get("/api/permissions", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["permissions"] == SAMPLE_PERMISSIONS
    mock_client.list_permissions.assert_called_once()


@pytest.mark.anyio
@patch("app.routers.permissions.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/permissions",
        headers=AUTH_HEADER,
        json={"name": "reports.read", "description": "View reports"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "reports.read"
    assert data["description"] == "View reports"
    mock_client.create_permission.assert_called_once_with("reports.read", "View reports")


@pytest.mark.anyio
@patch("app.routers.permissions.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission_default_description(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/permissions",
        headers=AUTH_HEADER,
        json={"name": "reports.read"},
    )
    assert response.status_code == 201
    mock_client.create_permission.assert_called_once_with("reports.read", "")


@pytest.mark.anyio
@patch("app.routers.permissions.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_permission(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.put(
        "/api/permissions/reports.read",
        headers=AUTH_HEADER,
        json={"new_name": "reports.view", "description": "View reports"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "reports.view"
    assert data["description"] == "View reports"
    mock_client.update_permission.assert_called_once_with("reports.read", "reports.view", "View reports")


@pytest.mark.anyio
@patch("app.routers.permissions.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_permission(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.delete("/api/permissions/reports.read", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "deleted"
    assert data["name"] == "reports.read"
    mock_client.delete_permission.assert_called_once_with("reports.read")


# --- Error handling ---


def _make_http_status_error(status_code: int = 500) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/permission/create")
    response = httpx.Response(status_code, request=request, text="error detail")
    return httpx.HTTPStatusError(f"{status_code} Server Error", request=request, response=response)


@pytest.mark.anyio
@patch("app.routers.permissions.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission_duplicate_returns_client_error(mock_validate, mock_factory, client):
    """Descope returns 4xx for duplicate permission names — forwarded to caller."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_permission.side_effect = _make_http_status_error(400)
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/permissions",
        headers=AUTH_HEADER,
        json={"name": "existing.perm"},
    )
    assert response.status_code == 400


@pytest.mark.anyio
@patch("app.routers.permissions.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_permissions_descope_server_error(mock_validate, mock_factory, client):
    """Descope 5xx → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_permissions.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.get("/api/permissions", headers=AUTH_HEADER)
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.permissions.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission_network_error(mock_validate, mock_factory, client):
    """Network error → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_permission.side_effect = httpx.RequestError("Connection refused")
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/permissions",
        headers=AUTH_HEADER,
        json={"name": "test.perm"},
    )
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.permissions.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_permission_descope_error(mock_validate, mock_factory, client):
    """Descope 5xx on update → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.update_permission.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.put(
        "/api/permissions/test.perm",
        headers=AUTH_HEADER,
        json={"new_name": "test.perm2"},
    )
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.permissions.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_permission_descope_error(mock_validate, mock_factory, client):
    """Descope 5xx on delete → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.delete_permission.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.delete("/api/permissions/test.perm", headers=AUTH_HEADER)
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.permissions.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_permission_network_error(mock_validate, mock_factory, client):
    """Network error on delete → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.delete_permission.side_effect = httpx.RequestError("Connection refused")
    mock_factory.return_value = mock_client

    response = await client.delete("/api/permissions/test.perm", headers=AUTH_HEADER)
    assert response.status_code == 502


# --- No tenant context ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_permissions_rejected_without_tenant(mock_validate, client):
    mock_validate.return_value = {"sub": "user789", "tenants": {}}
    response = await client.get("/api/permissions", headers=AUTH_HEADER)
    assert response.status_code == 403
