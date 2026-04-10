"""Unit tests for the providers router (Story 4.2).

Tests cover:
- Auth enforcement: unauthenticated → 401, wrong role → 403, no tenant → 403
- Happy paths: list, register, deactivate, capabilities
- Error handling: Conflict → 409, NotFound → 404
- Input validation: invalid UUID → 422, active=true rejected
- Security: config_ref stripped from all responses
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_provider_service
from app.errors.identity import Conflict, NotFound
from app.main import app
from app.services.provider import ProviderService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
def mock_provider_service():
    return AsyncMock(spec=ProviderService)


@pytest.fixture(autouse=True)
def _override_services(mock_provider_service):
    app.dependency_overrides[get_provider_service] = lambda: mock_provider_service
    yield
    app.dependency_overrides.pop(get_provider_service, None)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


TENANT_ID = "51c5957b-684a-453f-8ab1-8f239999c4d8"

OPERATOR_CLAIMS = {
    "sub": "operator1",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["operator"], "permissions": []},
    },
}

ADMIN_CLAIMS = {
    "sub": "admin1",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["admin"], "permissions": []},
    },
}

VIEWER_CLAIMS = {
    "sub": "viewer1",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["viewer"], "permissions": []},
    },
}

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

SAMPLE_PROVIDER = {
    "id": str(uuid.uuid4()),
    "name": "descope-prod",
    "type": "descope",
    "issuer_url": "https://api.descope.com/P123",
    "base_url": "https://api.descope.com",
    "capabilities": ["sso", "mfa"],
    "active": True,
}


# --- Auth enforcement ---


@pytest.mark.anyio
async def test_list_providers_rejects_unauthenticated(client):
    response = await client.get("/api/providers")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_register_provider_rejects_unauthenticated(client):
    response = await client.post("/api/providers", json={"name": "test", "type": "descope"})
    assert response.status_code == 401


@pytest.mark.anyio
async def test_deactivate_provider_rejects_unauthenticated(client):
    pid = str(uuid.uuid4())
    response = await client.patch(f"/api/providers/{pid}", json={"active": False})
    assert response.status_code == 401


@pytest.mark.anyio
async def test_capabilities_rejects_unauthenticated(client):
    pid = str(uuid.uuid4())
    response = await client.get(f"/api/providers/{pid}/capabilities")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_providers_rejected_for_admin(mock_validate, client):
    """Operator-only endpoints reject admin role."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/providers", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_register_provider_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/providers",
        headers=AUTH_HEADER,
        json={"name": "test", "type": "descope"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_providers_rejected_without_tenant(mock_validate, client):
    mock_validate.return_value = {"sub": "user1", "tenants": {}}
    response = await client.get("/api/providers", headers=AUTH_HEADER)
    assert response.status_code == 403


# --- Happy paths ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_providers(mock_validate, mock_provider_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    providers = [SAMPLE_PROVIDER, {**SAMPLE_PROVIDER, "name": "ory-prod"}]
    mock_provider_service.list_providers.return_value = Ok(providers)

    response = await client.get("/api/providers", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2
    mock_provider_service.list_providers.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_providers_empty(mock_validate, mock_provider_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    mock_provider_service.list_providers.return_value = Ok([])

    response = await client.get("/api/providers", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_register_provider(mock_validate, mock_provider_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    # Service layer now strips config_ref before returning
    mock_provider_service.register_provider.return_value = Ok(SAMPLE_PROVIDER)

    response = await client.post(
        "/api/providers",
        headers=AUTH_HEADER,
        json={"name": "descope-prod", "type": "descope", "config_ref": "infisical://secret"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "descope-prod"
    assert "config_ref" not in data
    mock_provider_service.register_provider.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_provider(mock_validate, mock_provider_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    pid = str(uuid.uuid4())
    # Service layer now strips config_ref before returning
    mock_provider_service.deactivate_provider.return_value = Ok({**SAMPLE_PROVIDER, "id": pid, "active": False})

    response = await client.patch(
        f"/api/providers/{pid}",
        headers=AUTH_HEADER,
        json={"active": False},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["active"] is False
    assert "config_ref" not in data
    mock_provider_service.deactivate_provider.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_capabilities(mock_validate, mock_provider_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    pid = str(uuid.uuid4())
    mock_provider_service.get_provider_capabilities.return_value = Ok(["sso", "mfa", "rbac"])

    response = await client.get(
        f"/api/providers/{pid}/capabilities",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    assert response.json() == ["sso", "mfa", "rbac"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_capabilities_empty(mock_validate, mock_provider_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    pid = str(uuid.uuid4())
    mock_provider_service.get_provider_capabilities.return_value = Ok([])

    response = await client.get(
        f"/api/providers/{pid}/capabilities",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    assert response.json() == []


# --- Error handling ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_register_provider_conflict(mock_validate, mock_provider_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    mock_provider_service.register_provider.return_value = Error(
        Conflict(message="Provider 'descope-prod' already exists")
    )

    response = await client.post(
        "/api/providers",
        headers=AUTH_HEADER,
        json={"name": "descope-prod", "type": "descope"},
    )
    assert response.status_code == 409


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_provider_not_found(mock_validate, mock_provider_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    pid = str(uuid.uuid4())
    mock_provider_service.deactivate_provider.return_value = Error(NotFound(message=f"Provider '{pid}' not found"))

    response = await client.patch(
        f"/api/providers/{pid}",
        headers=AUTH_HEADER,
        json={"active": False},
    )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_capabilities_not_found(mock_validate, mock_provider_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    pid = str(uuid.uuid4())
    mock_provider_service.get_provider_capabilities.return_value = Error(
        NotFound(message=f"Provider '{pid}' not found")
    )

    response = await client.get(
        f"/api/providers/{pid}/capabilities",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 404


# --- Input validation ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_invalid_uuid_returns_422(mock_validate, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    response = await client.patch(
        "/api/providers/not-a-uuid",
        headers=AUTH_HEADER,
        json={"active": False},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_capabilities_invalid_uuid_returns_422(mock_validate, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    response = await client.get(
        "/api/providers/not-a-uuid/capabilities",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_active_true_rejected(mock_validate, client):
    """PATCH /providers/{id} only supports active=false."""
    mock_validate.return_value = OPERATOR_CLAIMS
    pid = str(uuid.uuid4())
    response = await client.patch(
        f"/api/providers/{pid}",
        headers=AUTH_HEADER,
        json={"active": True},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_register_provider_empty_name_rejected(mock_validate, client):
    """Name field requires min_length=1."""
    mock_validate.return_value = OPERATOR_CLAIMS
    response = await client.post(
        "/api/providers",
        headers=AUTH_HEADER,
        json={"name": "", "type": "descope"},
    )
    assert response.status_code == 422
