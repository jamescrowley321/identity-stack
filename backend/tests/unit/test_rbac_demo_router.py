"""Unit tests for the RBAC demo router endpoints."""

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
        "tenant-abc": {
            "roles": ["admin"],
            "permissions": ["projects.create", "projects.read", "members.invite"],
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

NO_TENANT_CLAIMS = {
    "sub": "user789",
    "tenants": {},
}

NON_DICT_TENANT_CLAIMS = {
    "sub": "user-bad",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": None,
    },
}


# --- /rbac/hierarchy (public, no auth) ---


@pytest.mark.anyio
async def test_hierarchy_returns_all_roles(client):
    response = await client.get("/api/rbac/hierarchy")
    assert response.status_code == 200
    data = response.json()
    assert "roles" in data
    roles = data["roles"]
    assert set(roles.keys()) == {"owner", "admin", "member", "viewer"}
    # Owner should have billing.manage, viewer should not
    assert "billing.manage" in roles["owner"]["permissions"]
    assert "billing.manage" not in roles["viewer"]["permissions"]


@pytest.mark.anyio
async def test_hierarchy_no_auth_required(client):
    """Hierarchy endpoint should work without any auth header."""
    response = await client.get("/api/rbac/hierarchy")
    assert response.status_code == 200


@pytest.mark.anyio
async def test_hierarchy_member_is_default(client):
    """Member role should be documented as default."""
    response = await client.get("/api/rbac/hierarchy")
    data = response.json()
    assert "default" in data["roles"]["member"]["description"].lower()


# --- /rbac/effective (requires auth) ---


@pytest.mark.anyio
async def test_effective_rejects_unauthenticated(client):
    response = await client.get("/api/rbac/effective")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_effective_returns_admin_permissions(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/rbac/effective", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == "user123"
    assert data["tenant_id"] == "tenant-abc"
    assert "admin" in data["roles"]
    assert "projects.create" in data["permissions"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_effective_returns_viewer_permissions(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get("/api/rbac/effective", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["roles"] == ["viewer"]
    assert "documents.read" in data["permissions"]
    assert "billing.manage" not in data["permissions"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_effective_403_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.get("/api/rbac/effective", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 403


# --- /rbac/check/{permission} (requires auth) ---


@pytest.mark.anyio
async def test_check_rejects_unauthenticated(client):
    response = await client.get("/api/rbac/check/projects.read")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_allowed_permission(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/rbac/check/projects.create", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["permission"] == "projects.create"
    assert data["allowed"] is True


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_denied_permission(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get("/api/rbac/check/billing.manage", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["permission"] == "billing.manage"
    assert data["allowed"] is False


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_403_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.get("/api/rbac/check/projects.read", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 403


# --- Edge cases: non-dict tenant info ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_effective_handles_non_dict_tenant_info(mock_validate, client):
    """When tenant info is None (corrupt claim), return empty roles/permissions."""
    mock_validate.return_value = NON_DICT_TENANT_CLAIMS
    response = await client.get("/api/rbac/effective", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["roles"] == []
    assert data["permissions"] == []


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_handles_non_dict_tenant_info(mock_validate, client):
    """When tenant info is None, permission check should return allowed=False."""
    mock_validate.return_value = NON_DICT_TENANT_CLAIMS
    response = await client.get("/api/rbac/check/projects.read", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    assert response.json()["allowed"] is False


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_nonexistent_permission(mock_validate, client):
    """Checking a permission that doesn't exist in the system should return allowed=False."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/rbac/check/totally.fake.perm", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    data = response.json()
    assert data["permission"] == "totally.fake.perm"
    assert data["allowed"] is False
