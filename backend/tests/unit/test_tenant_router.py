"""Unit tests for the tenant router endpoints — Story 2.3 partial rewire.

create_tenant and get_current_tenant now use IdentityService.
list_user_tenants stays JWT-based. Resource endpoints stay DB-direct.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.dependencies.identity import get_identity_service
from app.errors.identity import Conflict, NotFound
from app.main import app
from app.models.database import get_async_session

TENANT_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

MOCK_CLAIMS_ADMIN = {
    "sub": "user123",
    "dct": TENANT_UUID,
    "roles": ["admin"],
    "tenants": {
        TENANT_UUID: {"roles": ["admin"], "permissions": ["read", "write"]},
        "b2c3d4e5-f6a7-8901-bcde-f12345678901": {"roles": ["viewer"], "permissions": ["read"]},
    },
}

MOCK_CLAIMS_WITH_TENANT = {
    "sub": "user123",
    "dct": TENANT_UUID,
    "tenants": {
        TENANT_UUID: {"roles": ["admin"], "permissions": ["read", "write"]},
        "b2c3d4e5-f6a7-8901-bcde-f12345678901": {"roles": ["viewer"], "permissions": ["read"]},
    },
}

MOCK_CLAIMS_NO_TENANT = {
    "sub": "user123",
    "tenants": {
        TENANT_UUID: {"roles": ["admin"], "permissions": ["read"]},
        "b2c3d4e5-f6a7-8901-bcde-f12345678901": {"roles": ["viewer"], "permissions": ["read"]},
    },
}


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


@pytest.fixture(autouse=True)
async def _test_db():
    """Use an in-memory async SQLite database for resource tests."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_async_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = override_get_async_session
    yield
    app.dependency_overrides.pop(get_async_session, None)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# =============================================================================
# list_user_tenants — JWT-based (unchanged)
# =============================================================================


@pytest.mark.anyio
async def test_list_tenants_rejects_unauthenticated(client):
    response = await client.get("/api/tenants")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_user_tenants_empty(mock_validate, client):
    """User with no tenants claim should get an empty list."""
    mock_validate.return_value = {"sub": "user123"}
    response = await client.get("/api/tenants", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json()["tenants"] == []


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_user_tenants(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.get("/api/tenants", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert len(data["tenants"]) == 2
    ids = {t["id"] for t in data["tenants"]}
    assert TENANT_UUID in ids


# =============================================================================
# get_current_tenant — now uses IdentityService
# =============================================================================


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_current_tenant(mock_validate, mock_service, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    tenant_dict = {"id": TENANT_UUID, "name": "Acme Corp"}
    mock_service.get_tenant.return_value = Ok(tenant_dict)

    response = await client.get("/api/tenants/current", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == TENANT_UUID
    assert data["tenant"]["name"] == "Acme Corp"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_current_tenant_returns_403_without_dct(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_NO_TENANT
    response = await client.get("/api/tenants/current", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_current_tenant_returns_null_on_not_found(mock_validate, mock_service, client):
    """When IdentityService returns NotFound, return tenant_id with null tenant."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    mock_service.get_tenant.return_value = Error(NotFound(message="Tenant not found"))

    response = await client.get("/api/tenants/current", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == TENANT_UUID
    assert data["tenant"] is None


# =============================================================================
# create_tenant — now uses IdentityService
# =============================================================================


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant(mock_validate, mock_service, client):
    mock_validate.return_value = MOCK_CLAIMS_ADMIN
    tenant_dict = {"id": "99999999-9999-9999-9999-999999999999", "name": "New Org"}
    mock_service.create_tenant.return_value = Ok(tenant_dict)

    response = await client.post(
        "/api/tenants",
        headers=AUTH_HEADER,
        json={"name": "New Org"},
    )
    assert response.status_code == 201
    assert response.json()["name"] == "New Org"
    mock_service.create_tenant.assert_awaited_once_with(
        name="New Org",
        domains=None,
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant_with_domains(mock_validate, mock_service, client):
    mock_validate.return_value = MOCK_CLAIMS_ADMIN
    tenant_dict = {"id": "99999999-9999-9999-9999-999999999999", "name": "Domain Corp"}
    mock_service.create_tenant.return_value = Ok(tenant_dict)

    response = await client.post(
        "/api/tenants",
        headers=AUTH_HEADER,
        json={"name": "Domain Corp", "self_provisioning_domains": ["domain.com"]},
    )
    assert response.status_code == 201
    mock_service.create_tenant.assert_awaited_once_with(
        name="Domain Corp",
        domains=["domain.com"],
    )


@pytest.mark.anyio
async def test_create_tenant_rejects_unauthenticated(client):
    response = await client.post("/api/tenants", json={"name": "Sneaky"})
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant_rejects_non_admin(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT  # no top-level roles
    response = await client.post(
        "/api/tenants",
        headers=AUTH_HEADER,
        json={"name": "Sneaky Org"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant_rejects_empty_name(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_ADMIN
    response = await client.post(
        "/api/tenants",
        headers=AUTH_HEADER,
        json={"name": ""},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant_conflict(mock_validate, mock_service, client):
    """Duplicate tenant name → 409."""
    mock_validate.return_value = MOCK_CLAIMS_ADMIN
    mock_service.create_tenant.return_value = Error(Conflict(message="duplicate"))

    response = await client.post(
        "/api/tenants",
        headers=AUTH_HEADER,
        json={"name": "Existing"},
    )
    assert response.status_code == 409


# =============================================================================
# Tenant resources — DB-direct, unchanged
# =============================================================================


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_tenant_resources_empty(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.get(
        f"/api/tenants/{TENANT_UUID}/resources",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    assert response.json()["resources"] == []


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_and_list_tenant_resources(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT

    create_resp = await client.post(
        f"/api/tenants/{TENANT_UUID}/resources",
        headers=AUTH_HEADER,
        json={"name": "Test Resource", "description": "A test"},
    )
    assert create_resp.status_code == 200
    resource = create_resp.json()
    assert resource["name"] == "Test Resource"
    assert resource["tenant_id"] == TENANT_UUID

    list_resp = await client.get(
        f"/api/tenants/{TENANT_UUID}/resources",
        headers=AUTH_HEADER,
    )
    assert list_resp.status_code == 200
    resources = list_resp.json()["resources"]
    assert len(resources) >= 1
    assert any(r["name"] == "Test Resource" for r in resources)


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_cannot_access_non_member_tenant_resources(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.get(
        "/api/tenants/tenant-other/resources",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_cannot_create_resource_in_non_member_tenant(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.post(
        "/api/tenants/tenant-other/resources",
        headers=AUTH_HEADER,
        json={"name": "Sneaky Resource"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_can_access_member_tenant_resources_without_dct(mock_validate, client):
    """User can access resources for a tenant they belong to, even if dct points elsewhere."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    other_member_tenant = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
    response = await client.get(
        f"/api/tenants/{other_member_tenant}/resources",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_resource_rejects_empty_name(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.post(
        f"/api/tenants/{TENANT_UUID}/resources",
        headers=AUTH_HEADER,
        json={"name": ""},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_resources_with_pagination(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.get(
        f"/api/tenants/{TENANT_UUID}/resources?limit=10&offset=0",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_resource_integrity_error_returns_409(mock_validate, client):
    """IntegrityError (e.g. duplicate name) returns 409 Conflict."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT

    async def _integrity_error_session():
        mock_session = MagicMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock(
            side_effect=IntegrityError("UNIQUE constraint failed", params=None, orig=Exception())
        )
        mock_session.rollback = AsyncMock()
        mock_session.refresh = AsyncMock()
        yield mock_session

    app.dependency_overrides[get_async_session] = _integrity_error_session

    response = await client.post(
        "/api/tenants/tenant-abc/resources",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "Duplicate Resource"},
    )
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_resource_db_error_returns_500(mock_validate, client):
    """Generic DB error returns 500."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT

    async def _db_error_session():
        mock_session = MagicMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock(side_effect=Exception("connection lost"))
        mock_session.rollback = AsyncMock()
        mock_session.refresh = AsyncMock()
        yield mock_session

    app.dependency_overrides[get_async_session] = _db_error_session

    response = await client.post(
        "/api/tenants/tenant-abc/resources",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "Some Resource"},
    )
    assert response.status_code == 500
