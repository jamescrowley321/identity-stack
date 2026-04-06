"""Unit tests for the users (member management) router.

Story 2.3: tests rewired endpoints that use UserService via DI
instead of get_descope_client().
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_user_service
from app.errors.identity import Conflict, NotFound
from app.main import app
from app.services.user import UserService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
def mock_user_service():
    return AsyncMock(spec=UserService)


@pytest.fixture(autouse=True)
def _override_user_service(mock_user_service):
    app.dependency_overrides[get_user_service] = lambda: mock_user_service
    yield
    app.dependency_overrides.pop(get_user_service, None)


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

SAMPLE_USER = {
    "id": str(uuid.uuid4()),
    "email": "alice@example.com",
    "user_name": "alice",
    "given_name": "Alice",
    "family_name": "Smith",
    "status": "active",
}


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
async def test_list_members(mock_validate, mock_user_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    users = [SAMPLE_USER, {**SAMPLE_USER, "email": "bob@example.com"}]
    mock_user_service.search_users.return_value = Ok(users)

    response = await client.get("/api/members", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert len(response.json()) == 2
    mock_user_service.search_users.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member(mock_validate, mock_user_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_user_service.create_user.return_value = Ok(SAMPLE_USER)

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["member"]},
    )
    assert response.status_code == 201
    mock_user_service.create_user.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member(mock_validate, mock_user_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_id = str(uuid.uuid4())
    mock_user_service.deactivate_user.return_value = Ok({**SAMPLE_USER, "status": "inactive"})

    response = await client.post(
        f"/api/members/{user_id}/deactivate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    mock_user_service.deactivate_user.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_activate_member(mock_validate, mock_user_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_id = str(uuid.uuid4())
    mock_user_service.activate_user.return_value = Ok(SAMPLE_USER)

    response = await client.post(
        f"/api/members/{user_id}/activate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    mock_user_service.activate_user.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_member(mock_validate, mock_user_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_id = str(uuid.uuid4())
    mock_user_service.deactivate_user.return_value = Ok({**SAMPLE_USER, "status": "inactive"})

    response = await client.delete(
        f"/api/members/{user_id}",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    mock_user_service.deactivate_user.assert_awaited_once()


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
async def test_owner_can_assign_owner_role(mock_validate, mock_user_service, client):
    mock_validate.return_value = OWNER_CLAIMS
    mock_user_service.create_user.return_value = Ok(SAMPLE_USER)

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["owner"]},
    )
    assert response.status_code == 201


# --- Error handling (service returns Error) ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_conflict(mock_validate, mock_user_service, client):
    """Duplicate email → 409 via result_to_response."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_user_service.create_user.return_value = Error(
        Conflict(message="User with email 'dup@test.com' already exists")
    )

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "dup@test.com"},
    )
    assert response.status_code == 409


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member_not_found(mock_validate, mock_user_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_id = str(uuid.uuid4())
    mock_user_service.deactivate_user.return_value = Error(NotFound(message=f"User '{user_id}' not found"))

    response = await client.post(
        f"/api/members/{user_id}/deactivate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_activate_member_not_found(mock_validate, mock_user_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_id = str(uuid.uuid4())
    mock_user_service.activate_user.return_value = Error(NotFound(message=f"User '{user_id}' not found"))

    response = await client.post(
        f"/api/members/{user_id}/activate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 404


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
