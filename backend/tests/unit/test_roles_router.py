"""Unit tests for the roles router endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

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


# --- M1: Role hierarchy enforcement tests ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_admin_cannot_assign_admin_role(mock_validate, client):
    """Admin should not be able to assign admin role (same level)."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["admin"]},
    )
    assert response.status_code == 403
    assert "admin" in response.json()["detail"].lower()


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_owner_can_assign_admin_role(mock_validate, mock_factory, client):
    """Owner should be able to assign admin role."""
    mock_validate.return_value = OWNER_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["admin"]},
    )
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_admin_cannot_remove_admin_role(mock_validate, client):
    """Admin should not be able to remove admin role (same level)."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/remove",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["admin"]},
    )
    assert response.status_code == 403


# --- M3: Invalid role name validation ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invalid_role_name_rejected(mock_validate, client):
    """Unknown role names should be rejected with 422."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["superadmin"]},
    )
    assert response.status_code == 422


# --- M2: Descope API error handling ---


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_descope_4xx_returns_400(mock_validate, mock_factory, client):
    """Descope 4xx errors should map to 400 with generic message."""
    mock_validate.return_value = OWNER_CLAIMS
    mock_client = AsyncMock()
    mock_response = MagicMock(status_code=404)
    mock_client.assign_roles.side_effect = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=mock_response
    )
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["member"]},
    )
    assert response.status_code == 400
    assert "invalid request" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_descope_5xx_returns_502(mock_validate, mock_factory, client):
    """Descope 5xx errors should map to 502 with generic message."""
    mock_validate.return_value = OWNER_CLAIMS
    mock_client = AsyncMock()
    mock_response = MagicMock(status_code=500)
    mock_client.assign_roles.side_effect = httpx.HTTPStatusError(
        "Server Error", request=MagicMock(), response=mock_response
    )
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/roles/assign",
        headers={"Authorization": "Bearer valid.token"},
        json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["member"]},
    )
    assert response.status_code == 502
    assert "upstream service error" in response.json()["detail"]
