"""Unit tests for the users (member management) router — Story 2.3 rewire.

Routers now inject IdentityService via Depends(get_identity_service) and return
Result types mapped through result_to_response(). Tests mock the service, not
the Descope client.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_identity_service
from app.errors.identity import NotFound, ProviderError
from app.main import app

TENANT_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
USER_UUID = "11111111-1111-1111-1111-111111111111"
ROLE_UUID = "22222222-2222-2222-2222-222222222222"

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

ADMIN_CLAIMS = {
    "sub": "user123",
    "dct": TENANT_UUID,
    "tenants": {
        TENANT_UUID: {"roles": ["admin"], "permissions": ["members.invite", "members.remove"]},
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": TENANT_UUID,
    "tenants": {
        TENANT_UUID: {"roles": ["viewer"], "permissions": ["projects.read"]},
    },
}

OWNER_CLAIMS = {
    "sub": "owner1",
    "dct": TENANT_UUID,
    "tenants": {
        TENANT_UUID: {"roles": ["owner"], "permissions": ["members.invite", "members.remove"]},
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


# --- Auth enforcement ---


@pytest.mark.anyio
async def test_list_members_rejects_unauthenticated(client):
    response = await client.get("/api/members")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_members_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get("/api/members", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_without_tenant_rejected(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com"},
    )
    assert response.status_code == 403


# --- Happy path ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_members(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    members = [
        {"id": str(uuid.uuid4()), "email": "alice@test.com", "status": "active"},
        {"id": str(uuid.uuid4()), "email": "bob@test.com", "status": "inactive"},
    ]
    mock_service.get_tenant_users_with_roles.return_value = Ok(members)

    response = await client.get("/api/members", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json()["members"] == members
    mock_service.get_tenant_users_with_roles.assert_awaited_once_with(
        tenant_id=uuid.UUID(TENANT_UUID),
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_dict = {"id": USER_UUID, "email": "new@test.com"}
    role_dict = {"id": ROLE_UUID, "name": "member"}
    mock_service.create_user.return_value = Ok(user_dict)
    mock_service.get_role_by_name.return_value = Ok(role_dict)
    mock_service.assign_role_to_user.return_value = Ok(None)

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["member"]},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "invited"
    assert data["email"] == "new@test.com"
    assert data["user"] == user_dict
    mock_service.create_user.assert_awaited_once_with(
        tenant_id=uuid.UUID(TENANT_UUID),
        email="new@test.com",
        user_name="new@test.com",
    )
    mock_service.get_role_by_name.assert_awaited_once_with(name="member")
    mock_service.assign_role_to_user.assert_awaited_once_with(
        tenant_id=uuid.UUID(TENANT_UUID),
        user_id=uuid.UUID(USER_UUID),
        role_id=uuid.UUID(ROLE_UUID),
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_with_default_role(mock_validate, mock_service, client):
    """Invite without specifying role should default to 'member'."""
    mock_validate.return_value = ADMIN_CLAIMS
    user_dict = {"id": USER_UUID, "email": "default@test.com"}
    role_dict = {"id": ROLE_UUID, "name": "member"}
    mock_service.create_user.return_value = Ok(user_dict)
    mock_service.get_role_by_name.return_value = Ok(role_dict)
    mock_service.assign_role_to_user.return_value = Ok(None)

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "default@test.com"},
    )
    assert response.status_code == 201
    mock_service.get_role_by_name.assert_awaited_once_with(name="member")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.deactivate_user.return_value = Ok({"id": USER_UUID, "status": "inactive"})

    response = await client.post(
        f"/api/members/{USER_UUID}/deactivate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "deactivated"
    mock_service.deactivate_user.assert_awaited_once_with(
        tenant_id=uuid.UUID(TENANT_UUID),
        user_id=uuid.UUID(USER_UUID),
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_activate_member(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.activate_user.return_value = Ok({"id": USER_UUID, "status": "active"})

    response = await client.post(
        f"/api/members/{USER_UUID}/activate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "activated"
    mock_service.activate_user.assert_awaited_once_with(
        tenant_id=uuid.UUID(TENANT_UUID),
        user_id=uuid.UUID(USER_UUID),
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_member(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.remove_user_from_tenant.return_value = Ok(None)

    response = await client.delete(
        f"/api/members/{USER_UUID}",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "removed"
    mock_service.remove_user_from_tenant.assert_awaited_once_with(
        tenant_id=uuid.UUID(TENANT_UUID),
        user_id=uuid.UUID(USER_UUID),
    )


# --- Owner role guard ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_admin_cannot_assign_owner_role(mock_validate, client):
    """Admin cannot invite a user with the owner role."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["owner"]},
    )
    assert response.status_code == 403
    assert "Only owners can assign the owner role" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_owner_can_assign_owner_role(mock_validate, mock_service, client):
    """Owner CAN invite a user with the owner role."""
    mock_validate.return_value = OWNER_CLAIMS
    user_dict = {"id": USER_UUID, "email": "new@test.com"}
    role_dict = {"id": ROLE_UUID, "name": "owner"}
    mock_service.create_user.return_value = Ok(user_dict)
    mock_service.get_role_by_name.return_value = Ok(role_dict)
    mock_service.assign_role_to_user.return_value = Ok(None)

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["owner"]},
    )
    assert response.status_code == 201


# --- Error handling: service returns Error → RFC 9457 Problem Detail ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_members_not_found(mock_validate, mock_service, client):
    """Tenant not found in DB → 404 Problem Detail."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.get_tenant_users_with_roles.return_value = Error(
        NotFound(message="Tenant not found"),
    )

    response = await client.get("/api/members", headers=AUTH_HEADER)
    assert response.status_code == 404
    data = response.json()
    assert data["type"] == "/errors/not-found"
    assert data["title"] == "Resource Not Found"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member_not_found(mock_validate, mock_service, client):
    """User not found in tenant → 404."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.deactivate_user.return_value = Error(
        NotFound(message=f"User '{USER_UUID}' not found"),
    )

    response = await client.post(
        f"/api/members/{USER_UUID}/deactivate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_activate_member_not_found(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.activate_user.return_value = Error(
        NotFound(message=f"User '{USER_UUID}' not found"),
    )

    response = await client.post(
        f"/api/members/{USER_UUID}/activate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_member_not_found(mock_validate, mock_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.remove_user_from_tenant.return_value = Error(
        NotFound(message="No roles in tenant"),
    )

    response = await client.delete(
        f"/api/members/{USER_UUID}",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_provider_error(mock_validate, mock_service, client):
    """Provider error during create_user → 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.create_user.return_value = Error(
        ProviderError(message="upstream failed"),
    )

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["member"]},
    )
    assert response.status_code == 502
    assert response.json()["type"] == "/errors/provider-error"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_role_not_found(mock_validate, mock_service, client):
    """If get_role_by_name returns NotFound, invite fails with 404."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_service.create_user.return_value = Ok({"id": USER_UUID, "email": "new@test.com"})
    mock_service.get_role_by_name.return_value = Error(
        NotFound(message="Role 'nonexistent' not found"),
    )

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["nonexistent"]},
    )
    assert response.status_code == 404


# --- Input validation ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_invalid_email_rejected(mock_validate, client):
    """Invalid email strings are rejected by pydantic EmailStr."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "not-an-email", "role_names": ["member"]},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member_invalid_uuid(mock_validate, client):
    """Invalid user_id UUID → 422."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/members/not-a-uuid/deactivate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 422
