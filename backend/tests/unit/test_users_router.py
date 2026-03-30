"""Unit tests for the users (member management) router."""

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
        "tenant-abc": {"roles": ["admin"], "permissions": ["members.invite", "members.remove"]},
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["viewer"], "permissions": ["projects.read"]},
    },
}


@pytest.mark.anyio
async def test_list_members_rejects_unauthenticated(client):
    response = await client.get("/api/members")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_members_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get("/api/members", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_members(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.search_tenant_users.return_value = [
        {"userId": "u1", "name": "Alice", "email": "alice@test.com", "status": "enabled"},
        {"userId": "u2", "name": "Bob", "email": "bob@test.com", "status": "disabled"},
    ]
    mock_factory.return_value = mock_client

    response = await client.get("/api/members", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    assert len(response.json()["members"]) == 2
    mock_client.search_tenant_users.assert_called_once_with("tenant-abc")


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.invite_user.return_value = {"userId": "new-user", "email": "new@test.com"}
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/members/invite",
        headers={"Authorization": "Bearer valid.token"},
        json={"email": "new@test.com", "role_names": ["member"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "invited"
    mock_client.invite_user.assert_called_once_with("new@test.com", "tenant-abc", ["member"])


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/members/invite",
        headers={"Authorization": "Bearer valid.token"},
        json={"email": "new@test.com"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_user.return_value = {
        "userId": "user1",
        "loginIds": ["user1@example.com"],
        "userTenants": [{"tenantId": "tenant-abc"}],
    }
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/members/user1/deactivate",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "deactivated"
    mock_client.load_user.assert_called_once_with("user1")
    mock_client.update_user_status.assert_called_once_with("user1@example.com", "disabled")


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_activate_member(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_user.return_value = {
        "userId": "user1",
        "loginIds": ["user1@example.com"],
        "userTenants": [{"tenantId": "tenant-abc"}],
    }
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/members/user1/activate",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "activated"
    mock_client.load_user.assert_called_once_with("user1")
    mock_client.update_user_status.assert_called_once_with("user1@example.com", "enabled")


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_member(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_user.return_value = {
        "userId": "user1",
        "loginIds": ["user1@example.com"],
        "userTenants": [{"tenantId": "tenant-abc"}],
    }
    mock_factory.return_value = mock_client

    response = await client.delete(
        "/api/members/user1",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "removed"
    mock_client.load_user.assert_called_once_with("user1")
    mock_client.remove_user_from_tenant.assert_called_once_with("user1@example.com", "tenant-abc")


OWNER_CLAIMS = {
    "sub": "owner1",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["owner"], "permissions": ["members.invite", "members.remove"]},
    },
}


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member_cross_tenant_rejected(mock_validate, mock_factory, client):
    """M1: Admin cannot deactivate a user who belongs to a different tenant."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_user.return_value = {"userId": "user1", "userTenants": [{"tenantId": "other-tenant"}]}
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/members/user1/deactivate",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert response.status_code == 403
    assert "does not belong to your tenant" in response.json()["detail"]
    mock_client.update_user_status.assert_not_called()


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_member_cross_tenant_rejected(mock_validate, mock_factory, client):
    """M1: Admin cannot remove a user from a different tenant."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_user.return_value = {"userId": "user1", "userTenants": [{"tenantId": "other-tenant"}]}
    mock_factory.return_value = mock_client

    response = await client.delete(
        "/api/members/user1",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert response.status_code == 403
    mock_client.remove_user_from_tenant.assert_not_called()


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_activate_member_cross_tenant_rejected(mock_validate, mock_factory, client):
    """M1: Admin cannot activate a user who belongs to a different tenant."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_user.return_value = {"userId": "user1", "userTenants": [{"tenantId": "other-tenant"}]}
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/members/user1/activate",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert response.status_code == 403
    assert "does not belong to your tenant" in response.json()["detail"]
    mock_client.update_user_status.assert_not_called()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_invalid_email_rejected(mock_validate, client):
    """M3: Invalid email strings are rejected by pydantic EmailStr."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/members/invite",
        headers={"Authorization": "Bearer valid.token"},
        json={"email": "not-an-email", "role_names": ["member"]},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_admin_cannot_assign_owner_role(mock_validate, client):
    """M4: Admin cannot invite a user with the owner role."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/members/invite",
        headers={"Authorization": "Bearer valid.token"},
        json={"email": "new@test.com", "role_names": ["owner"]},
    )
    assert response.status_code == 403
    assert "Only owners can assign the owner role" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_owner_can_assign_owner_role(mock_validate, mock_factory, client):
    """M4: Owner CAN invite a user with the owner role."""
    mock_validate.return_value = OWNER_CLAIMS
    mock_client = AsyncMock()
    mock_client.invite_user.return_value = {"userId": "new", "email": "new@test.com"}
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/members/invite",
        headers={"Authorization": "Bearer valid.token"},
        json={"email": "new@test.com", "role_names": ["owner"]},
    )
    assert response.status_code == 200
    mock_client.invite_user.assert_called_once_with("new@test.com", "tenant-abc", ["owner"])


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_without_tenant_rejected(mock_validate, client):
    mock_validate.return_value = {"sub": "user789", "tenants": {}}
    response = await client.post(
        "/api/members/invite",
        headers={"Authorization": "Bearer valid.token"},
        json={"email": "new@test.com"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_with_default_role(mock_validate, mock_factory, client):
    """Invite without specifying role should default to 'member'."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.invite_user.return_value = {"userId": "new", "email": "default@test.com"}
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/members/invite",
        headers={"Authorization": "Bearer valid.token"},
        json={"email": "default@test.com"},
    )
    assert response.status_code == 200
    mock_client.invite_user.assert_called_once_with("default@test.com", "tenant-abc", ["member"])
