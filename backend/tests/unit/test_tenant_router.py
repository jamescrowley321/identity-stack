"""Unit tests for the tenant router endpoints."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.main import app
from app.models.database import get_session
from app.models.tenant import TenantResource

# Only create SQLite-compatible tables (excludes identity tables with PostgreSQL ARRAY/JSONB)
_SQLITE_TABLES = [TenantResource.__table__]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture(autouse=True)
async def _test_db():
    """Use an in-memory SQLite database for each test."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=_SQLITE_TABLES)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    yield
    app.dependency_overrides.pop(get_session, None)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all, tables=_SQLITE_TABLES)
    await engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


MOCK_CLAIMS_ADMIN = {
    "sub": "user123",
    "dct": "tenant-abc",
    "roles": ["admin"],
    "tenants": {
        "tenant-abc": {"roles": ["admin"], "permissions": ["read", "write"]},
        "tenant-xyz": {"roles": ["viewer"], "permissions": ["read"]},
    },
}

MOCK_CLAIMS_WITH_TENANT = {
    "sub": "user123",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["admin"], "permissions": ["read", "write"]},
        "tenant-xyz": {"roles": ["viewer"], "permissions": ["read"]},
    },
}

MOCK_CLAIMS_NO_TENANT = {
    "sub": "user123",
    "tenants": {
        "tenant-abc": {"roles": ["admin"], "permissions": ["read"]},
        "tenant-xyz": {"roles": ["viewer"], "permissions": ["read"]},
    },
}


@pytest.mark.anyio
async def test_list_tenants_rejects_unauthenticated(client):
    response = await client.get("/api/tenants")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_user_tenants_empty(mock_validate, client):
    """User with no tenants claim should get an empty list."""
    mock_validate.return_value = {"sub": "user123"}
    response = await client.get("/api/tenants", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    assert response.json()["tenants"] == []


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_user_tenants(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.get("/api/tenants", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    data = response.json()
    assert len(data["tenants"]) == 2
    ids = {t["id"] for t in data["tenants"]}
    assert "tenant-abc" in ids
    assert "tenant-xyz" in ids


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_current_tenant(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    with patch("app.routers.tenants.get_descope_client") as mock_factory:
        mock_client = AsyncMock()
        mock_client.load_tenant.return_value = {"id": "tenant-abc", "name": "Acme Corp"}
        mock_factory.return_value = mock_client

        response = await client.get("/api/tenants/current", headers={"Authorization": "Bearer valid.token"})
        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "tenant-abc"
        assert data["tenant"]["name"] == "Acme Corp"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_current_tenant_returns_403_without_dct(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_NO_TENANT
    response = await client.get("/api/tenants/current", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_current_tenant_returns_null_on_404(mock_validate, client):
    """When Descope API returns 404 for tenant, return tenant_id with null tenant."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    with patch("app.routers.tenants.get_descope_client") as mock_factory:
        mock_client = AsyncMock()
        req = httpx.Request("POST", "http://test")
        response_404 = httpx.Response(404, request=req)
        mock_client.load_tenant.side_effect = httpx.HTTPStatusError("Not Found", request=req, response=response_404)
        mock_factory.return_value = mock_client

        response = await client.get("/api/tenants/current", headers={"Authorization": "Bearer valid.token"})
        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "tenant-abc"
        assert data["tenant"] is None


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_current_tenant_returns_502_on_api_error(mock_validate, client):
    """When Descope API returns a non-404 error, return 502."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    with patch("app.routers.tenants.get_descope_client") as mock_factory:
        mock_client = AsyncMock()
        req = httpx.Request("POST", "http://test")
        response_500 = httpx.Response(500, request=req)
        mock_client.load_tenant.side_effect = httpx.HTTPStatusError("Server Error", request=req, response=response_500)
        mock_factory.return_value = mock_client

        response = await client.get("/api/tenants/current", headers={"Authorization": "Bearer valid.token"})
        assert response.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_current_tenant_returns_502_on_network_error(mock_validate, client):
    """When a network error occurs, return 502."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    with patch("app.routers.tenants.get_descope_client") as mock_factory:
        mock_client = AsyncMock()
        mock_client.load_tenant.side_effect = httpx.RequestError("Connection refused")
        mock_factory.return_value = mock_client

        response = await client.get("/api/tenants/current", headers={"Authorization": "Bearer valid.token"})
        assert response.status_code == 502


@pytest.mark.anyio
@patch("app.routers.tenants.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant(mock_validate, mock_factory, client):
    mock_validate.return_value = MOCK_CLAIMS_ADMIN
    mock_client = AsyncMock()
    mock_client.create_tenant.return_value = {"id": "new-tenant-id"}
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/tenants",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "New Org"},
    )
    assert response.status_code == 200
    assert response.json()["id"] == "new-tenant-id"


@pytest.mark.anyio
@patch("app.routers.tenants.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant_with_domains(mock_validate, mock_factory, client):
    """Create tenant with self-provisioning domains."""
    mock_validate.return_value = MOCK_CLAIMS_ADMIN
    mock_client = AsyncMock()
    mock_client.create_tenant.return_value = {"id": "domain-tenant"}
    mock_factory.return_value = mock_client

    response = await client.post(
        "/api/tenants",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "Domain Corp", "self_provisioning_domains": ["domain.com"]},
    )
    assert response.status_code == 200
    mock_client.create_tenant.assert_called_once_with(name="Domain Corp", self_provisioning_domains=["domain.com"])


@pytest.mark.anyio
async def test_create_tenant_rejects_unauthenticated(client):
    response = await client.post("/api/tenants", json={"name": "Sneaky"})
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant_rejects_non_admin(mock_validate, client):
    """User without admin/owner role cannot create tenants."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT  # no top-level roles
    response = await client.post(
        "/api/tenants",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "Sneaky Org"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant_rejects_empty_name(mock_validate, client):
    """Empty tenant name should be rejected by validation."""
    mock_validate.return_value = MOCK_CLAIMS_ADMIN
    response = await client.post(
        "/api/tenants",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": ""},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_tenant_resources_empty(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.get(
        "/api/tenants/tenant-abc/resources",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert response.status_code == 200
    assert response.json()["resources"] == []


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_and_list_tenant_resources(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT

    # Create a resource
    create_resp = await client.post(
        "/api/tenants/tenant-abc/resources",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "Test Resource", "description": "A test"},
    )
    assert create_resp.status_code == 200
    resource = create_resp.json()
    assert resource["name"] == "Test Resource"
    assert resource["tenant_id"] == "tenant-abc"

    # List resources
    list_resp = await client.get(
        "/api/tenants/tenant-abc/resources",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert list_resp.status_code == 200
    resources = list_resp.json()["resources"]
    assert len(resources) >= 1
    assert any(r["name"] == "Test Resource" for r in resources)


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_cannot_access_non_member_tenant_resources(mock_validate, client):
    """User who is not a member of a tenant cannot access its resources."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT  # member of tenant-abc and tenant-xyz
    response = await client.get(
        "/api/tenants/tenant-other/resources",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_cannot_create_resource_in_non_member_tenant(mock_validate, client):
    """User who is not a member of a tenant cannot create resources in it."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT  # member of tenant-abc and tenant-xyz
    response = await client.post(
        "/api/tenants/tenant-other/resources",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "Sneaky Resource"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_can_access_member_tenant_resources_without_dct(mock_validate, client):
    """User can access resources for a tenant they are a member of, even if dct points elsewhere."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT  # dct=tenant-abc, member of tenant-xyz too
    response = await client.get(
        "/api/tenants/tenant-xyz/resources",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_resource_rejects_empty_name(mock_validate, client):
    """Empty resource name should be rejected by validation."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.post(
        "/api/tenants/tenant-abc/resources",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": ""},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_resources_with_pagination(mock_validate, client):
    """Pagination params are accepted."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.get(
        "/api/tenants/tenant-abc/resources?limit=10&offset=0",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert response.status_code == 200
