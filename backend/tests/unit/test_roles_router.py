"""Unit tests for the roles router endpoints."""

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
        "tenant-abc": {
            "roles": ["admin"],
            "permissions": ["projects.create", "projects.read", "members.invite", "members.update_role"],
        },
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {
            "roles": ["viewer"],
            "permissions": ["projects.read", "documents.read"],
        },
    },
}

OWNER_CLAIMS = {
    "sub": "owner1",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {
            "roles": ["owner"],
            "permissions": ["projects.create", "members.update_role", "billing.manage"],
        },
    },
}

NO_TENANT_CLAIMS = {
    "sub": "user789",
    "tenants": {},
}


@pytest.mark.anyio
async def test_roles_me_rejects_unauthenticated(client):
    response = await client.get("/api/roles/me")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_roles_me_returns_current_roles(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/roles/me", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == "tenant-abc"
    assert "admin" in data["roles"]
    assert "projects.create" in data["permissions"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_roles_me_returns_403_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.get("/api/roles/me", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_as_admin(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["member"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "roles_assigned"
    mock_client.assign_roles.assert_called_once_with("target-user", "tenant-abc", ["member"])


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["admin"]},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_roles_as_admin(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles/remove",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["member"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "roles_removed"
    mock_client.remove_roles.assert_called_once_with("target-user", "tenant-abc", ["member"])


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_roles_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/roles/remove",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["admin"]},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_roles_me_viewer(mock_validate, client):
    """Viewer should see their own roles and permissions."""
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get("/api/roles/me", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    data = response.json()
    assert "viewer" in data["roles"]
    assert "documents.read" in data["permissions"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_rejected_without_tenant(mock_validate, client):
    """Role assignment should fail when user has no tenant context."""
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["member"]},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_rejected_cross_tenant(mock_validate, client):
    """Admin in tenant-abc cannot assign roles in tenant-other."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-other", "role_names": ["member"]},
    )
    assert response.status_code == 403
    assert "different tenant" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_admin_cannot_assign_owner_role(mock_validate, client):
    """Admin should not be able to escalate to owner."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["owner"]},
    )
    assert response.status_code == 403
    assert "owner" in response.json()["detail"].lower()


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_owner_can_assign_owner_role(mock_validate, mock_factory, client):
    """Owner should be able to assign the owner role."""
    mock_validate.return_value = OWNER_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["owner"]},
    )
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_empty_role_names_rejected(mock_validate, client):
    """Empty role_names list should be rejected by validation."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": []},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_roles_rejected_cross_tenant(mock_validate, client):
    """Admin cannot remove roles in a different tenant."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/remove",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-other", "role_names": ["member"]},
    )
    assert response.status_code == 403


# =============================================================================
# Role Definition CRUD Tests
# =============================================================================

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

SAMPLE_ROLES = [
    {"name": "editor", "description": "Can edit", "permissionNames": ["docs.write"]},
    {"name": "viewer", "description": "Read only", "permissionNames": ["docs.read"]},
]


def _make_http_status_error(status_code: int = 500) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/role/create")
    response = httpx.Response(status_code, request=request, text="error detail")
    return httpx.HTTPStatusError(f"{status_code} Server Error", request=request, response=response)


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


# --- Happy path ---


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_roles(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_roles.return_value = SAMPLE_ROLES
    mock_factory.return_value = mock_client

    response = await client.get("/api/roles", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["roles"] == SAMPLE_ROLES
    mock_client.list_roles.assert_called_once()


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "editor", "description": "Can edit", "permission_names": ["docs.write"]},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "editor"
    assert data["description"] == "Can edit"
    assert data["permission_names"] == ["docs.write"]
    mock_client.create_role.assert_called_once_with("editor", "Can edit", ["docs.write"])


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_default_fields(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "viewer"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["description"] == ""
    assert data["permission_names"] == []
    mock_client.create_role.assert_called_once_with("viewer", "", None)


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.put(
        "/api/roles/editor",
        headers=AUTH_HEADER,
        json={
            "new_name": "senior-editor",
            "description": "Senior editor",
            "permission_names": ["docs.write", "docs.admin"],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "senior-editor"
    assert data["description"] == "Senior editor"
    assert data["permission_names"] == ["docs.write", "docs.admin"]
    mock_client.update_role.assert_called_once_with(
        "editor", "senior-editor", "Senior editor", ["docs.write", "docs.admin"]
    )


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_role(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.delete("/api/roles/editor", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "deleted"
    assert data["name"] == "editor"
    mock_client.delete_role.assert_called_once_with("editor")


# --- Partial update ---


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role_partial_description_only(mock_validate, mock_factory, client):
    """Omitting new_name and permission_names should not wipe them."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.put(
        "/api/roles/editor",
        headers=AUTH_HEADER,
        json={"description": "Updated desc"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "editor"  # defaults to path param
    assert data["description"] == "Updated desc"
    assert data["permission_names"] is None  # not sent → unchanged
    mock_client.update_role.assert_called_once_with("editor", "editor", "Updated desc", None)


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role_empty_body(mock_validate, mock_factory, client):
    """Empty body is a no-op: name defaults to path param, others are None."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.put(
        "/api/roles/editor",
        headers=AUTH_HEADER,
        json={},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "editor"
    mock_client.update_role.assert_called_once_with("editor", "editor", None, None)


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_empty_permission_name_rejected(mock_validate, client):
    """Empty strings in permission_names list should be rejected."""
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
    """Empty strings in permission_names list should be rejected on update."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.put(
        "/api/roles/editor",
        headers=AUTH_HEADER,
        json={"permission_names": ["", "docs.read"]},
    )
    assert response.status_code == 422


# --- Error handling ---


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_duplicate_returns_client_error(mock_validate, mock_factory, client):
    """Descope returns 400/409 for duplicate role names — forwarded to caller."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_role.side_effect = _make_http_status_error(409)
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "existing-role"},
    )
    assert response.status_code == 409


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_bad_request_forwarded(mock_validate, mock_factory, client):
    """Descope 400 on create → forwarded."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_role.side_effect = _make_http_status_error(400)
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "bad-role"},
    )
    assert response.status_code == 400


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_roles_descope_server_error(mock_validate, mock_factory, client):
    """Descope 5xx → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_roles.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.get("/api/roles", headers=AUTH_HEADER)
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_network_error(mock_validate, mock_factory, client):
    """Network error → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_role.side_effect = httpx.RequestError("Connection refused")
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "test-role"},
    )
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role_descope_error(mock_validate, mock_factory, client):
    """Descope 5xx on update → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.update_role.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.put(
        "/api/roles/editor",
        headers=AUTH_HEADER,
        json={"new_name": "senior-editor"},
    )
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role_not_found(mock_validate, mock_factory, client):
    """Descope 404 on update → forwarded."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.update_role.side_effect = _make_http_status_error(404)
    mock_factory.return_value = mock_client

    response = await client.put(
        "/api/roles/nonexistent",
        headers=AUTH_HEADER,
        json={"new_name": "new-name"},
    )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_role_descope_error(mock_validate, mock_factory, client):
    """Descope 5xx on delete → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.delete_role.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.delete("/api/roles/editor", headers=AUTH_HEADER)
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_role_not_found(mock_validate, mock_factory, client):
    """Descope 404 on delete → forwarded."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.delete_role.side_effect = _make_http_status_error(404)
    mock_factory.return_value = mock_client

    response = await client.delete("/api/roles/nonexistent", headers=AUTH_HEADER)
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_role_network_error(mock_validate, mock_factory, client):
    """Network error on delete → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.delete_role.side_effect = httpx.RequestError("Connection refused")
    mock_factory.return_value = mock_client

    response = await client.delete("/api/roles/editor", headers=AUTH_HEADER)
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role_network_error(mock_validate, mock_factory, client):
    """Network error on update → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.update_role.side_effect = httpx.RequestError("Connection refused")
    mock_factory.return_value = mock_client

    response = await client.put(
        "/api/roles/editor",
        headers=AUTH_HEADER,
        json={"new_name": "senior-editor"},
    )
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_roles_network_error(mock_validate, mock_factory, client):
    """Network error on list → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_roles.side_effect = httpx.RequestError("Connection refused")
    mock_factory.return_value = mock_client

    response = await client.get("/api/roles", headers=AUTH_HEADER)
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_descope_401_becomes_502(mock_validate, mock_factory, client):
    """Descope 401 (e.g. expired mgmt key) should not be forwarded — returns 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_role.side_effect = _make_http_status_error(401)
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "test-role"},
    )
    assert response.status_code == 502


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


# --- No tenant context ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_roles_rejected_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.get("/api/roles", headers=AUTH_HEADER)
    assert response.status_code == 403


# --- Route ordering: /roles/me still works ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_roles_me_not_captured_by_name_param(mock_validate, client):
    """Ensure /roles/me is NOT interpreted as /roles/{name} with name='me'."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/roles/me", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    # /roles/me returns tenant_id, roles, permissions — not a role definition
    assert "tenant_id" in data
    assert "roles" in data
