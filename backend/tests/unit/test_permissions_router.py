"""Unit tests for the permissions router.

Story 2.3: tests rewired endpoints that use PermissionService via DI
instead of get_descope_client().
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_permission_service
from app.errors.identity import Conflict
from app.main import app
from app.services.permission import PermissionService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
def mock_permission_service():
    return AsyncMock(spec=PermissionService)


@pytest.fixture(autouse=True)
def _override_permission_service(mock_permission_service):
    app.dependency_overrides[get_permission_service] = lambda: mock_permission_service
    yield
    app.dependency_overrides.pop(get_permission_service, None)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


TENANT_ID = "51c5957b-684a-453f-8ab1-8f239999c4d8"

ADMIN_CLAIMS = {
    "sub": "user123",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["admin"], "permissions": ["settings.manage"]},
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["viewer"], "permissions": ["projects.read"]},
    },
}

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

PERM_ID = str(uuid.uuid4())

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
    mock_validate.return_value = {"sub": "user789", "tenants": {}}
    response = await client.get("/api/permissions", headers=AUTH_HEADER)
    assert response.status_code == 403


# --- Happy path ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_permissions(mock_validate, mock_permission_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_permission_service.list_permissions.return_value = Ok(SAMPLE_PERMISSIONS)

    response = await client.get("/api/permissions", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    mock_permission_service.list_permissions.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission(mock_validate, mock_permission_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    perm_dict = {"id": PERM_ID, "name": "reports.read", "description": "View reports"}
    mock_permission_service.create_permission.return_value = Ok(perm_dict)

    response = await client.post(
        "/api/permissions",
        headers=AUTH_HEADER,
        json={"name": "reports.read", "description": "View reports"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "reports.read"
    assert data["description"] == "View reports"
    mock_permission_service.create_permission.assert_awaited_once_with(name="reports.read", description="View reports")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission_default_description(mock_validate, mock_permission_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    perm_dict = {"id": PERM_ID, "name": "reports.read", "description": ""}
    mock_permission_service.create_permission.return_value = Ok(perm_dict)

    response = await client.post(
        "/api/permissions",
        headers=AUTH_HEADER,
        json={"name": "reports.read"},
    )
    assert response.status_code == 201
    mock_permission_service.create_permission.assert_awaited_once_with(name="reports.read", description="")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_permission(mock_validate, mock_permission_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    perm_id = str(uuid.uuid4())
    mock_permission_service.list_permissions.return_value = Ok([{"id": perm_id, "name": "reports.read"}])
    mock_permission_service.update_permission.return_value = Ok(
        {"id": perm_id, "name": "reports.view", "description": "View reports"}
    )

    response = await client.put(
        "/api/permissions/reports.read",
        headers=AUTH_HEADER,
        json={"new_name": "reports.view", "description": "View reports"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "reports.view"
    mock_permission_service.update_permission.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_permission(mock_validate, mock_permission_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    perm_id = str(uuid.uuid4())
    mock_permission_service.list_permissions.return_value = Ok([{"id": perm_id, "name": "reports.read"}])
    mock_permission_service.delete_permission.return_value = Ok({"status": "deleted", "name": "reports.read"})

    response = await client.delete("/api/permissions/reports.read", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "deleted"
    assert data["name"] == "reports.read"


# --- Error handling ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_permission_duplicate_returns_conflict(mock_validate, mock_permission_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_permission_service.create_permission.return_value = Error(
        Conflict(message="Permission 'existing.perm' already exists")
    )

    response = await client.post(
        "/api/permissions",
        headers=AUTH_HEADER,
        json={"name": "existing.perm"},
    )
    assert response.status_code == 409


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_permission_not_found(mock_validate, mock_permission_service, client):
    """Permission name not in list → 404."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_permission_service.list_permissions.return_value = Ok([])

    response = await client.put(
        "/api/permissions/nonexistent",
        headers=AUTH_HEADER,
        json={"new_name": "new-name"},
    )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_permission_not_found(mock_validate, mock_permission_service, client):
    """Permission name not in list → 404."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_permission_service.list_permissions.return_value = Ok([])

    response = await client.delete("/api/permissions/nonexistent", headers=AUTH_HEADER)
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
