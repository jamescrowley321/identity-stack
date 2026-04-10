"""Unit tests for the roles router endpoints.

Story 2.3: tests rewired endpoints that use RoleService via DI
instead of get_descope_client().
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_role_service
from app.errors.identity import Conflict, SyncFailed
from app.main import app
from app.services.role import RoleService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
def mock_role_service():
    return AsyncMock(spec=RoleService)


@pytest.fixture(autouse=True)
def _override_role_service(mock_role_service):
    app.dependency_overrides[get_role_service] = lambda: mock_role_service
    yield
    app.dependency_overrides.pop(get_role_service, None)


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
        TENANT_ID: {
            "roles": ["admin"],
            "permissions": ["projects.create", "projects.read", "members.invite", "members.update_role"],
        },
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {
            "roles": ["viewer"],
            "permissions": ["projects.read", "documents.read"],
        },
    },
}

OWNER_CLAIMS = {
    "sub": "owner1",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {
            "roles": ["owner"],
            "permissions": ["projects.create", "members.update_role", "billing.manage"],
        },
    },
}

NO_TENANT_CLAIMS = {
    "sub": "user789",
    "tenants": {},
}

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

ROLE_ID = str(uuid.uuid4())

SAMPLE_ROLES = [
    {"id": str(uuid.uuid4()), "name": "editor", "description": "Can edit", "tenant_id": None},
    {"id": str(uuid.uuid4()), "name": "viewer", "description": "Read only", "tenant_id": None},
]


# --- /roles/me (claims-based, no service) ---


@pytest.mark.anyio
async def test_roles_me_rejects_unauthenticated(client):
    response = await client.get("/api/roles/me")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_roles_me_returns_current_roles(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/roles/me", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == TENANT_ID
    assert "admin" in data["roles"]
    assert "projects.create" in data["permissions"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_roles_me_returns_403_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.get("/api/roles/me", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_roles_me_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get("/api/roles/me", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert "viewer" in data["roles"]
    assert "documents.read" in data["permissions"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_roles_me_not_captured_by_name_param(mock_validate, client):
    """Ensure /roles/me is NOT interpreted as /roles/{name} with name='me'."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/roles/me", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert "tenant_id" in data
    assert "roles" in data


# --- Auth enforcement for CRUD endpoints ---


@pytest.mark.anyio
async def test_list_roles_rejects_unauthenticated(client):
    response = await client.get("/api/roles")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_roles_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get("/api/roles", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post("/api/roles", headers=AUTH_HEADER, json={"name": "test-role"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.put("/api/roles/editor", headers=AUTH_HEADER, json={"new_name": "senior-editor"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_role_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.delete("/api/roles/editor", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_roles_rejected_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.get("/api/roles", headers=AUTH_HEADER)
    assert response.status_code == 403


# --- Role assign/remove auth ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": TENANT_ID, "role_names": ["admin"]},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_roles_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/roles/remove",
        headers=AUTH_HEADER,
        json={"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": TENANT_ID, "role_names": ["admin"]},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_rejected_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": TENANT_ID, "role_names": ["member"]},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_rejected_cross_tenant(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": "tenant-other", "role_names": ["member"]},
    )
    assert response.status_code == 403
    assert "different tenant" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_admin_cannot_assign_owner_role(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": TENANT_ID, "role_names": ["owner"]},
    )
    assert response.status_code == 403
    assert "owner" in response.json()["detail"].lower()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_empty_role_names_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": TENANT_ID, "role_names": []},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_roles_rejected_cross_tenant(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/remove",
        headers=AUTH_HEADER,
        json={"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": "tenant-other", "role_names": ["member"]},
    )
    assert response.status_code == 403


# --- Happy path ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_roles(mock_validate, mock_role_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_role_service.list_roles.return_value = Ok(SAMPLE_ROLES)

    response = await client.get("/api/roles", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert "roles" in data
    assert len(data["roles"]) == 2
    mock_role_service.list_roles.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role(mock_validate, mock_role_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    role_dict = {"id": ROLE_ID, "name": "editor", "description": "Can edit", "tenant_id": None}
    mock_role_service.create_role.return_value = Ok(role_dict)

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "editor", "description": "Can edit"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "editor"
    mock_role_service.create_role.assert_awaited_once_with(name="editor", description="Can edit", permission_names=None)


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_default_fields(mock_validate, mock_role_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    role_dict = {"id": ROLE_ID, "name": "viewer", "description": "", "tenant_id": None}
    mock_role_service.create_role.return_value = Ok(role_dict)

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "viewer"},
    )
    assert response.status_code == 201
    mock_role_service.create_role.assert_awaited_once_with(name="viewer", description="", permission_names=None)


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role(mock_validate, mock_role_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    role_id = str(uuid.uuid4())
    mock_role_service.list_roles.return_value = Ok([{"id": role_id, "name": "editor"}])
    mock_role_service.update_role.return_value = Ok(
        {"id": role_id, "name": "senior-editor", "description": "Senior editor", "tenant_id": None}
    )

    response = await client.put(
        "/api/roles/editor",
        headers=AUTH_HEADER,
        json={"new_name": "senior-editor", "description": "Senior editor"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "senior-editor"
    mock_role_service.update_role.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_role(mock_validate, mock_role_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    role_id = str(uuid.uuid4())
    mock_role_service.list_roles.return_value = Ok([{"id": role_id, "name": "editor"}])
    mock_role_service.delete_role.return_value = Ok({"status": "deleted", "name": "editor"})

    response = await client.delete("/api/roles/editor", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "deleted"
    assert data["name"] == "editor"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_as_admin(mock_validate, mock_role_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    role_id = str(uuid.uuid4())
    mock_role_service.list_roles.return_value = Ok([{"id": role_id, "name": "member"}])
    mock_role_service.assign_role_to_user.return_value = Ok(
        {"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": TENANT_ID, "role_id": role_id}
    )

    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": TENANT_ID, "role_names": ["member"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "roles_assigned"
    mock_role_service.assign_role_to_user.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_owner_can_assign_owner_role(mock_validate, mock_role_service, client):
    mock_validate.return_value = OWNER_CLAIMS
    role_id = str(uuid.uuid4())
    mock_role_service.list_roles.return_value = Ok([{"id": role_id, "name": "owner"}])
    mock_role_service.assign_role_to_user.return_value = Ok(
        {"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": TENANT_ID, "role_id": role_id}
    )

    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": TENANT_ID, "role_names": ["owner"]},
    )
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_roles_as_admin(mock_validate, mock_role_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    role_id = str(uuid.uuid4())
    mock_role_service.list_roles.return_value = Ok([{"id": role_id, "name": "member"}])
    user_id = "dd98f159-658f-47f3-9ed2-99e85686b04c"
    mock_role_service.unassign_role_from_user.return_value = Ok(
        {"status": "removed", "user_id": user_id, "tenant_id": TENANT_ID, "role_id": role_id}
    )

    response = await client.post(
        "/api/roles/remove",
        headers=AUTH_HEADER,
        json={"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": TENANT_ID, "role_names": ["member"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "roles_removed"


# --- Error handling ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_duplicate_returns_conflict(mock_validate, mock_role_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_role_service.create_role.return_value = Error(Conflict(message="Role 'editor' already exists in this scope"))

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "editor"},
    )
    assert response.status_code == 409


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role_not_found(mock_validate, mock_role_service, client):
    """Role name not found in list → 404."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_role_service.list_roles.return_value = Ok([])

    response = await client.put(
        "/api/roles/nonexistent",
        headers=AUTH_HEADER,
        json={"new_name": "new-name"},
    )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_role_not_found(mock_validate, mock_role_service, client):
    """Role name not found in list → 404."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_role_service.list_roles.return_value = Ok([])

    response = await client.delete("/api/roles/nonexistent", headers=AUTH_HEADER)
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_role_not_found(mock_validate, mock_role_service, client):
    """Role name not in list → 404."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_role_service.list_roles.return_value = Ok([])

    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": "dd98f159-658f-47f3-9ed2-99e85686b04c", "tenant_id": TENANT_ID, "role_names": ["nonexistent"]},
    )
    assert response.status_code == 404


# --- Input validation ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_empty_name_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": ""},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role_empty_new_name_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.put(
        "/api/roles/editor",
        headers=AUTH_HEADER,
        json={"new_name": ""},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_empty_permission_name_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "test-role", "permission_names": ["", "docs.read"]},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role_empty_permission_name_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.put(
        "/api/roles/editor",
        headers=AUTH_HEADER,
        json={"permission_names": ["", "docs.read"]},
    )
    assert response.status_code == 422


# --- SyncFailed → 207 Multi-Status ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_sync_failed_returns_207(mock_validate, mock_role_service, client):
    """Service returns SyncFailed (DB write ok, IdP sync failed) → 207 Multi-Status."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_role_service.create_role.return_value = Error(
        SyncFailed(
            message="Descope sync failed for role creation",
            operation="create_role",
            underlying_error="httpx.ConnectError",
        )
    )

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "new-role"},
    )
    assert response.status_code == 207
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "/errors/sync-failed"
    assert body["title"] == "Sync Partial Success"
    assert body["status"] == 207
    assert "succeeded" in body["detail"]
    assert "synchronisation failed" in body["detail"]
