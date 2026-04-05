"""Unit tests for the roles router endpoints — Story 2.3 rewire.

Routers now inject IdentityService via Depends(get_identity_service) and return
Result types mapped through result_to_response(). Tests mock the service, not
the Descope client. /roles/me stays JWT-based (no service).
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
USER_UUID = "11111111-1111-1111-1111-111111111111"
ROLE_UUID = "22222222-2222-2222-2222-222222222222"
PERM_UUID = "33333333-3333-3333-3333-333333333333"

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

ADMIN_CLAIMS = {
    "sub": "user123",
    "dct": TENANT_UUID,
    "tenants": {
        TENANT_UUID: {
            "roles": ["admin"],
            "permissions": ["projects.create", "projects.read", "members.invite", "members.update_role"],
        },
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": TENANT_UUID,
    "tenants": {
        TENANT_UUID: {"roles": ["viewer"], "permissions": ["projects.read", "documents.read"]},
    },
}

OWNER_CLAIMS = {
    "sub": "owner1",
    "dct": TENANT_UUID,
    "tenants": {
        TENANT_UUID: {
            "roles": ["owner"],
            "permissions": ["projects.create", "members.update_role", "billing.manage"],
        },
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


# =============================================================================
# /roles/me — JWT-based, no service
# =============================================================================


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
    assert data["tenant_id"] == TENANT_UUID
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
    """Viewer should see their own roles and permissions."""
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


# =============================================================================
# /roles/assign and /roles/remove
# =============================================================================


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_as_admin(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    role_dict = {"id": ROLE_UUID, "name": "member"}
    mock_service.get_role_by_name.return_value = Ok(role_dict)
    mock_service.assign_role_to_user.return_value = Ok(None)

    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": USER_UUID, "tenant_id": TENANT_UUID, "role_names": ["member"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "roles_assigned"
    mock_service.get_role_by_name.assert_awaited_once_with(name="member", tenant_id=uuid.UUID(TENANT_UUID))
    mock_service.assign_role_to_user.assert_awaited_once_with(
        tenant_id=uuid.UUID(TENANT_UUID),
        user_id=uuid.UUID(USER_UUID),
        role_id=uuid.UUID(ROLE_UUID),
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": USER_UUID, "tenant_id": TENANT_UUID, "role_names": ["admin"]},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_roles_as_admin(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    role_dict = {"id": ROLE_UUID, "name": "member"}
    mock_service.get_role_by_name.return_value = Ok(role_dict)
    mock_service.remove_role_from_user.return_value = Ok(None)

    response = await client.post(
        "/api/roles/remove",
        headers=AUTH_HEADER,
        json={"user_id": USER_UUID, "tenant_id": TENANT_UUID, "role_names": ["member"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "roles_removed"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_roles_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/roles/remove",
        headers=AUTH_HEADER,
        json={"user_id": USER_UUID, "tenant_id": TENANT_UUID, "role_names": ["admin"]},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_rejected_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": USER_UUID, "tenant_id": TENANT_UUID, "role_names": ["member"]},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_rejected_cross_tenant(mock_validate, client):
    """Admin in TENANT_UUID cannot assign roles in a different tenant."""
    mock_validate.return_value = ADMIN_CLAIMS
    other_tenant = str(uuid.uuid4())
    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": USER_UUID, "tenant_id": other_tenant, "role_names": ["member"]},
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
        headers=AUTH_HEADER,
        json={"user_id": USER_UUID, "tenant_id": TENANT_UUID, "role_names": ["owner"]},
    )
    assert response.status_code == 403
    assert "owner" in response.json()["detail"].lower()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_owner_can_assign_owner_role(mock_validate, mock_service, client):
    mock_validate.return_value = OWNER_CLAIMS
    role_dict = {"id": ROLE_UUID, "name": "owner"}
    mock_service.get_role_by_name.return_value = Ok(role_dict)
    mock_service.assign_role_to_user.return_value = Ok(None)

    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": USER_UUID, "tenant_id": TENANT_UUID, "role_names": ["owner"]},
    )
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_empty_role_names_rejected(mock_validate, client):
    """Empty role_names list should be rejected by validation."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": USER_UUID, "tenant_id": TENANT_UUID, "role_names": []},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_roles_rejected_cross_tenant(mock_validate, client):
    """Admin cannot remove roles in a different tenant."""
    mock_validate.return_value = ADMIN_CLAIMS
    other_tenant = str(uuid.uuid4())
    response = await client.post(
        "/api/roles/remove",
        headers=AUTH_HEADER,
        json={"user_id": USER_UUID, "tenant_id": other_tenant, "role_names": ["member"]},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_role_not_found(mock_validate, mock_service, client):
    """get_role_by_name returns NotFound → 404."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.get_role_by_name.return_value = Error(
        NotFound(message="Role 'nonexistent' not found"),
    )

    response = await client.post(
        "/api/roles/assign",
        headers=AUTH_HEADER,
        json={"user_id": USER_UUID, "tenant_id": TENANT_UUID, "role_names": ["nonexistent"]},
    )
    assert response.status_code == 404


# =============================================================================
# Role definition CRUD
# =============================================================================

SAMPLE_ROLES = [
    {"id": str(uuid.uuid4()), "name": "editor", "description": "Can edit"},
    {"id": str(uuid.uuid4()), "name": "viewer", "description": "Read only"},
]


# --- Auth enforcement for CRUD ---


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


# --- Happy path ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_roles(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.list_roles.return_value = Ok(SAMPLE_ROLES)

    response = await client.get("/api/roles", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json()["roles"] == SAMPLE_ROLES
    mock_service.list_roles.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    role_dict = {"id": ROLE_UUID, "name": "editor", "description": "Can edit"}
    mock_service.create_role.return_value = Ok(role_dict)

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "editor", "description": "Can edit"},
    )
    assert response.status_code == 201
    mock_service.create_role.assert_awaited_once_with(name="editor", description="Can edit")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_with_permissions(mock_validate, mock_service, client):
    """Creating role with permission_names maps each permission."""
    mock_validate.return_value = ADMIN_CLAIMS
    role_dict = {"id": ROLE_UUID, "name": "editor", "description": ""}
    perm_dict = {"id": PERM_UUID, "name": "docs.write"}
    mock_service.create_role.return_value = Ok(role_dict)
    mock_service.get_permission_by_name.return_value = Ok(perm_dict)
    mock_service.map_permission_to_role.return_value = Ok(None)

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "editor", "permission_names": ["docs.write"]},
    )
    assert response.status_code == 201
    mock_service.get_permission_by_name.assert_awaited_once_with(name="docs.write")
    mock_service.map_permission_to_role.assert_awaited_once_with(
        role_id=uuid.UUID(ROLE_UUID),
        permission_id=uuid.UUID(PERM_UUID),
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_default_fields(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    role_dict = {"id": ROLE_UUID, "name": "viewer", "description": ""}
    mock_service.create_role.return_value = Ok(role_dict)

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "viewer"},
    )
    assert response.status_code == 201
    mock_service.create_role.assert_awaited_once_with(name="viewer", description="")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.get_role_by_name.return_value = Ok({"id": ROLE_UUID, "name": "editor"})
    updated = {"id": ROLE_UUID, "name": "senior-editor", "description": "Senior editor"}
    mock_service.update_role.return_value = Ok(updated)

    response = await client.put(
        "/api/roles/editor",
        headers=AUTH_HEADER,
        json={"new_name": "senior-editor", "description": "Senior editor"},
    )
    assert response.status_code == 200
    mock_service.get_role_by_name.assert_awaited_once_with(name="editor")
    mock_service.update_role.assert_awaited_once_with(
        role_id=uuid.UUID(ROLE_UUID),
        name="senior-editor",
        description="Senior editor",
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role_empty_body(mock_validate, mock_service, client):
    """Empty body passes None for optional fields."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.get_role_by_name.return_value = Ok({"id": ROLE_UUID, "name": "editor"})
    mock_service.update_role.return_value = Ok({"id": ROLE_UUID, "name": "editor"})

    response = await client.put(
        "/api/roles/editor",
        headers=AUTH_HEADER,
        json={},
    )
    assert response.status_code == 200
    mock_service.update_role.assert_awaited_once_with(
        role_id=uuid.UUID(ROLE_UUID),
        name=None,
        description=None,
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_role(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.get_role_by_name.return_value = Ok({"id": ROLE_UUID, "name": "editor"})
    mock_service.delete_role.return_value = Ok(None)

    response = await client.delete("/api/roles/editor", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "deleted"
    assert data["name"] == "editor"
    mock_service.delete_role.assert_awaited_once_with(role_id=uuid.UUID(ROLE_UUID))


# --- Error handling ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_roles_provider_error(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.list_roles.return_value = Error(ProviderError(message="upstream failed"))

    response = await client.get("/api/roles", headers=AUTH_HEADER)
    assert response.status_code == 502
    assert response.json()["type"] == "/errors/provider-error"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_conflict(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.create_role.return_value = Error(Conflict(message="duplicate"))

    response = await client.post(
        "/api/roles",
        headers=AUTH_HEADER,
        json={"name": "existing-role"},
    )
    assert response.status_code == 409
    assert response.json()["type"] == "/errors/conflict"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_role_not_found(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.get_role_by_name.return_value = Error(
        NotFound(message="Role 'nonexistent' not found"),
    )

    response = await client.put(
        "/api/roles/nonexistent",
        headers=AUTH_HEADER,
        json={"new_name": "new-name"},
    )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_role_not_found(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.get_role_by_name.return_value = Error(
        NotFound(message="Role 'nonexistent' not found"),
    )

    response = await client.delete("/api/roles/nonexistent", headers=AUTH_HEADER)
    assert response.status_code == 404


# --- Input validation ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_role_empty_name_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post("/api/roles", headers=AUTH_HEADER, json={"name": ""})
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
