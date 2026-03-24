"""Unit tests for the tenant router endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session, SQLModel, create_engine

from app.main import app
from app.models.database import get_session


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture(autouse=True)
def _test_db():
    """Use an in-memory SQLite database for each test."""
    engine = create_engine("sqlite://", echo=False, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    yield
    app.dependency_overrides.pop(get_session, None)
    SQLModel.metadata.drop_all(engine)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


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
async def test_get_current_tenant_handles_descope_api_failure(mock_validate, client):
    """When Descope API fails to load tenant info, still return tenant_id with null tenant."""
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
    with patch("app.routers.tenants.get_descope_client") as mock_factory:
        mock_client = AsyncMock()
        mock_client.load_tenant.side_effect = Exception("API unavailable")
        mock_factory.return_value = mock_client

        response = await client.get("/api/tenants/current", headers={"Authorization": "Bearer valid.token"})
        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "tenant-abc"
        assert data["tenant"] is None


@pytest.mark.anyio
@patch("app.routers.tenants.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant(mock_validate, mock_factory, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
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
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT
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
async def test_cannot_access_other_tenant_resources(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT  # dct = tenant-abc
    response = await client.get(
        "/api/tenants/tenant-other/resources",
        headers={"Authorization": "Bearer valid.token"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_cannot_create_resource_in_other_tenant(mock_validate, client):
    mock_validate.return_value = MOCK_CLAIMS_WITH_TENANT  # dct = tenant-abc
    response = await client.post(
        "/api/tenants/tenant-other/resources",
        headers={"Authorization": "Bearer valid.token"},
        json={"name": "Sneaky Resource"},
    )
    assert response.status_code == 403
