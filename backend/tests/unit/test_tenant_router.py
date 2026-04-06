"""Unit tests for the tenant router endpoints.

Story 2.3: create_tenant and get_current_tenant now use TenantService via DI.
list_user_tenants stays claims-based. tenant resources stay direct SQLAlchemy.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.dependencies.identity import get_tenant_service
from app.errors.identity import NotFound
from app.main import app
from app.models.database import get_async_session
from app.services.tenant import TenantService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
def mock_tenant_service():
    return AsyncMock(spec=TenantService)


@pytest.fixture(autouse=True)
async def _test_db(mock_tenant_service):
    """Use an in-memory async SQLite database for each test."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_async_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = override_get_async_session
    app.dependency_overrides[get_tenant_service] = lambda: mock_tenant_service
    yield
    app.dependency_overrides.pop(get_async_session, None)
    app.dependency_overrides.pop(get_tenant_service, None)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


TENANT_UUID = "51c5957b-684a-453f-8ab1-8f239999c4d8"
TENANT_UUID_2 = "dd98f159-658f-47f3-9ed2-99e85686b04c"

MOCK_CLAIMS_ADMIN = {
    "sub": "user123",
    "dct": TENANT_UUID,
    "roles": ["admin"],
    "tenants": {
        TENANT_UUID: {"roles": ["admin"], "permissions": ["read", "write"]},
        TENANT_UUID_2: {"roles": ["viewer"], "permissions": ["read"]},
    },
}

MOCK_CLAIMS_WITH_TENANT = {
    "sub": "user123",
    "dct": TENANT_UUID,
    "tenants": {
        TENANT_UUID: {"roles": ["admin"], "permissions": ["read", "write"]},
        TENANT_UUID_2: {"roles": ["viewer"], "permissions": ["read"]},
    },
}

MOCK_CLAIMS_NO_TENANT = {
    "sub": "user123",
    "tenants": {
        TENANT_UUID: {"roles": ["admin"], "permissions": ["read"]},
        TENANT_UUID_2: {"roles": ["viewer"], "permissions": ["read"]},
    },
}

AUTH_HEADER = {"Authorization": "Bearer valid.token"}


# --- Claims-based endpoints (unchanged) ---


@pytest.mark.anyio
async def test_list_tenants_rejects_unauthenticated(client):
    response = await client.get("/api/tenants")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_user_tenants_empty(mock_validate, client):
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
    assert TENANT_UUID_2 in ids


# --- create_tenant (via TenantService) ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant(mock_validate, mock_tenant_service, client):
    mock_validate.return_value = MOCK_CLAIMS_ADMIN
    tenant_dict = {"id": "new-tenant-id", "name": "New Org", "domains": []}
    mock_tenant_service.create_tenant.return_value = Ok(tenant_dict)

    response = await client.post(
        "/api/tenants",
        headers=AUTH_HEADER,
        json={"name": "New Org"},
    )
    assert response.status_code == 201
    mock_tenant_service.create_tenant.assert_awaited_once_with(name="New Org", domains=None)


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant_with_domains(mock_validate, mock_tenant_service, client):
    mock_validate.return_value = MOCK_CLAIMS_ADMIN
    tenant_dict = {"id": "domain-tenant", "name": "Domain Corp", "domains": ["domain.com"]}
    mock_tenant_service.create_tenant.return_value = Ok(tenant_dict)

    response = await client.post(
        "/api/tenants",
        headers=AUTH_HEADER,
        json={"name": "Domain Corp", "self_provisioning_domains": ["domain.com"]},
    )
    assert response.status_code == 201
    mock_tenant_service.create_tenant.assert_awaited_once_with(name="Domain Corp", domains=["domain.com"])


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


# --- get_current_tenant (via TenantService) ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_current_tenant(mock_validate, mock_tenant_service, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    tenant_dict = {"id": TENANT_UUID, "name": "Acme Corp", "domains": []}
    mock_tenant_service.get_tenant.return_value = Ok(tenant_dict)

    response = await client.get("/api/tenants/current", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Acme Corp"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_current_tenant_returns_403_without_dct(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_NO_TENANT
    response = await client.get("/api/tenants/current", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_current_tenant_not_found(mock_validate, mock_tenant_service, client):
    """Tenant not found in canonical DB → 404."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    mock_tenant_service.get_tenant.return_value = Error(NotFound(message="Tenant 'tenant-abc' not found"))

    response = await client.get("/api/tenants/current", headers=AUTH_HEADER)
    assert response.status_code == 404


# --- Tenant resources (direct SQLAlchemy, unchanged) ---


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
        "/api/tenants/8a70c45c-dc5e-48ed-a8ea-34b0b35058a4/resources",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_cannot_create_resource_in_non_member_tenant(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.post(
        "/api/tenants/8a70c45c-dc5e-48ed-a8ea-34b0b35058a4/resources",
        headers=AUTH_HEADER,
        json={"name": "Sneaky Resource"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_can_access_member_tenant_resources_without_dct(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    response = await client.get(
        f"/api/tenants/{TENANT_UUID_2}/resources",
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
        f"/api/tenants/{TENANT_UUID}/resources",
        headers=AUTH_HEADER,
        json={"name": "Duplicate Resource"},
    )
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_resource_db_error_returns_500(mock_validate, client):
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
        f"/api/tenants/{TENANT_UUID}/resources",
        headers=AUTH_HEADER,
        json={"name": "Some Resource"},
    )
    assert response.status_code == 500
