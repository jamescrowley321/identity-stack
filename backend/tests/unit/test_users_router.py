"""Unit tests for the users (member management) router.

The members router calls the Descope Management API directly via
request.app.state.descope_client -- no UserService / RoleService DI.
"""

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
    # Reset rate limiter storage between tests to avoid 429 interference
    from app.middleware.rate_limit import limiter

    limiter.reset()


@pytest.fixture
def mock_descope():
    """Return the Descope client mock already set on app.state by conftest."""
    return app.state.descope_client


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
        TENANT_ID: {"roles": ["admin"], "permissions": ["members.invite", "members.remove"]},
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["viewer"], "permissions": ["projects.read"]},
    },
}

OWNER_CLAIMS = {
    "sub": "owner1",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["owner"], "permissions": ["members.invite", "members.remove"]},
    },
}

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

# Descope-style user dicts (as returned by the management API)
DESCOPE_USER = {
    "userId": "U2abc123",
    "email": "alice@example.com",
    "name": "Alice Smith",
    "status": "enabled",
    "userTenants": [
        {"tenantId": TENANT_ID, "roleNames": ["admin"]},
    ],
}


def _make_http_status_error(status_code: int = 500, body: str = "error") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/user")
    response = httpx.Response(status_code, request=request, text=body)
    return httpx.HTTPStatusError(f"{status_code} Server Error", request=request, response=response)


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
    mock_validate.return_value = {"sub": "user789", "tenants": {}}
    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com"},
    )
    assert response.status_code == 403


# --- Happy path ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_members(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.search_tenant_users.return_value = [
        DESCOPE_USER,
        {**DESCOPE_USER, "userId": "U2def456", "email": "bob@example.com", "name": "Bob"},
    ]

    response = await client.get("/api/members", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert "members" in data
    assert len(data["members"]) == 2
    mock_descope.search_tenant_users.assert_awaited_once_with(TENANT_ID)


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.invite_user.return_value = DESCOPE_USER

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["member"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "invited"
    assert data["email"] == "new@test.com"
    assert "user" in data
    mock_descope.invite_user.assert_awaited_once_with(
        email="new@test.com",
        tenant_id=TENANT_ID,
        role_names=["member"],
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_id = "U2abc123"
    mock_descope.update_user_status.return_value = None

    response = await client.post(
        f"/api/members/{user_id}/deactivate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "deactivated"
    assert data["user_id"] == user_id
    mock_descope.update_user_status.assert_awaited_once_with(user_id, "disabled")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_activate_member(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_id = "U2abc123"
    mock_descope.update_user_status.return_value = None

    response = await client.post(
        f"/api/members/{user_id}/activate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "activated"
    assert data["user_id"] == user_id
    mock_descope.update_user_status.assert_awaited_once_with(user_id, "enabled")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_member(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_id = "U2abc123"
    mock_descope.remove_user_from_tenant.return_value = None

    response = await client.delete(
        f"/api/members/{user_id}",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "removed"
    assert data["user_id"] == user_id
    mock_descope.remove_user_from_tenant.assert_awaited_once_with(user_id, TENANT_ID)


# --- Owner role escalation guard ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_admin_cannot_assign_owner_role(mock_validate, client):
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
async def test_owner_can_assign_owner_role(mock_validate, mock_descope, client):
    mock_validate.return_value = OWNER_CLAIMS
    mock_descope.invite_user.return_value = DESCOPE_USER

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["owner"]},
    )
    assert response.status_code == 200


# --- Descope API error handling ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_members_descope_error(mock_validate, mock_descope, client):
    """Descope API error -> 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.search_tenant_users.side_effect = _make_http_status_error(500)

    response = await client.get("/api/members", headers=AUTH_HEADER)
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_descope_400(mock_validate, mock_descope, client):
    """Descope 400 (e.g. duplicate) -> 400 with error description."""
    mock_validate.return_value = ADMIN_CLAIMS
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/user/create")
    response_obj = httpx.Response(
        400,
        request=request,
        json={"errorDescription": "User already exists"},
    )
    mock_descope.invite_user.side_effect = httpx.HTTPStatusError(
        "400 Bad Request", request=request, response=response_obj
    )

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "dup@test.com"},
    )
    assert response.status_code == 400
    assert "User already exists" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_descope_502(mock_validate, mock_descope, client):
    """Descope 500 -> 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.invite_user.side_effect = _make_http_status_error(500)

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com"},
    )
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member_descope_error(mock_validate, mock_descope, client):
    """Descope API error deactivating -> 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.update_user_status.side_effect = _make_http_status_error(500)

    response = await client.post(
        "/api/members/U2abc123/deactivate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_activate_member_descope_error(mock_validate, mock_descope, client):
    """Descope API error activating -> 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.update_user_status.side_effect = _make_http_status_error(500)

    response = await client.post(
        "/api/members/U2abc123/activate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_member_descope_error(mock_validate, mock_descope, client):
    """Descope API error removing -> 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.remove_user_from_tenant.side_effect = _make_http_status_error(500)

    response = await client.delete(
        "/api/members/U2abc123",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 502


# --- Input validation ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_invalid_email_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "not-an-email", "role_names": ["member"]},
    )
    assert response.status_code == 422
