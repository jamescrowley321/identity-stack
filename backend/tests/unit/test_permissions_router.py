"""Unit tests for the permissions router — Story 2.3 rewire.

All permission endpoints now use IdentityService via get_identity_service.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_identity_service
from app.errors.identity import Conflict, NotFound, ProviderError
from app.main import app

TENANT_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
PERM_UUID = "33333333-3333-3333-3333-333333333333"

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

ADMIN_CLAIMS = {
    "sub": "user123",
    "dct": TENANT_UUID,
    "tenants": {
        TENANT_UUID: {"roles": ["admin"], "permissions": ["settings.manage"]},
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": TENANT_UUID,
    "tenants": {
        TENANT_UUID: {"roles": ["viewer"], "permissions": ["projects.read"]},
    },
}

NO_TENANT_CLAIMS = {"sub": "user789", "tenants": {}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
def mock_service():
    return AsyncMock()


@pytest.fixture(autouse=True)
def _override_identity_service(mock_service):
    app.dependency_overrides[get_identity_service] = lambda: mock_service
    yield
    app.dependency_overrides.pop(get_identity_service, None)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


SAMPLE_PERMISSIONS = [
    {"id": str(uuid.uuid4()), "name": "reports.read", "description": "View reports"},
    {"id": str(uuid.uuid4()), "name": "reports.write", "description": "Edit reports"},
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


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_permissions_rejected_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.get("/api/permissions", headers=AUTH_HEADER)
    assert response.status_code == 403


# --- Happy path ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_permissions(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.list_permissions.return_value = Ok(SAMPLE_PERMISSIONS)

    response = await client.get("/api/permissions", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json()["permissions"] == SAMPLE_PERMISSIONS
    mock_service.list_permissions.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    perm_dict = {"id": PERM_UUID, "name": "reports.read", "description": "View reports"}
    mock_service.create_permission.return_value = Ok(perm_dict)

    response = await client.post(
        "/api/permissions",
        headers=AUTH_HEADER,
        json={"name": "reports.read", "description": "View reports"},
    )
    assert response.status_code == 201
    mock_service.create_permission.assert_awaited_once_with(
        name="reports.read",
        description="View reports",
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission_default_description(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    perm_dict = {"id": PERM_UUID, "name": "reports.read", "description": ""}
    mock_service.create_permission.return_value = Ok(perm_dict)

    response = await client.post(
        "/api/permissions",
        headers=AUTH_HEADER,
        json={"name": "reports.read"},
    )
    assert response.status_code == 201
    mock_service.create_permission.assert_awaited_once_with(name="reports.read", description="")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_permission(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.get_permission_by_name.return_value = Ok(
        {"id": PERM_UUID, "name": "reports.read"},
    )
    updated = {"id": PERM_UUID, "name": "reports.view", "description": "View reports"}
    mock_service.update_permission.return_value = Ok(updated)

    response = await client.put(
        "/api/permissions/reports.read",
        headers=AUTH_HEADER,
        json={"new_name": "reports.view", "description": "View reports"},
    )
    assert response.status_code == 200
    mock_service.get_permission_by_name.assert_awaited_once_with(name="reports.read")
    mock_service.update_permission.assert_awaited_once_with(
        permission_id=uuid.UUID(PERM_UUID),
        name="reports.view",
        description="View reports",
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_permission(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.get_permission_by_name.return_value = Ok(
        {"id": PERM_UUID, "name": "reports.read"},
    )
    mock_service.delete_permission.return_value = Ok(None)

    response = await client.delete("/api/permissions/reports.read", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "deleted"
    assert data["name"] == "reports.read"
    mock_service.delete_permission.assert_awaited_once_with(
        permission_id=uuid.UUID(PERM_UUID),
    )


# --- Error handling ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission_conflict(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.create_permission.return_value = Error(
        Conflict(message="duplicate name"),
    )

    response = await client.post(
        "/api/permissions",
        headers=AUTH_HEADER,
        json={"name": "existing.perm"},
    )
    assert response.status_code == 409
    assert response.json()["type"] == "/errors/conflict"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_permissions_provider_error(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.list_permissions.return_value = Error(
        ProviderError(message="upstream failed"),
    )

    response = await client.get("/api/permissions", headers=AUTH_HEADER)
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_permission_not_found(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.get_permission_by_name.return_value = Error(
        NotFound(message="Permission 'missing' not found"),
    )

    response = await client.put(
        "/api/permissions/missing",
        headers=AUTH_HEADER,
        json={"new_name": "new-name"},
    )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_permission_not_found(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.get_permission_by_name.return_value = Error(
        NotFound(message="Permission 'missing' not found"),
    )

    response = await client.delete("/api/permissions/missing", headers=AUTH_HEADER)
    assert response.status_code == 404


# --- Input validation ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission_empty_name_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/permissions",
        headers=AUTH_HEADER,
        json={"name": ""},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_permission_empty_new_name_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.put(
        "/api/permissions/test.perm",
        headers=AUTH_HEADER,
        json={"new_name": ""},
    )
    assert response.status_code == 422
