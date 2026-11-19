"""Unit tests for the attributes router (user profile + tenant settings)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_identity_resolution_service
from app.errors.identity import NotFound
from app.main import app
from app.models.database import get_async_session


def _make_http_status_error(status_code: int = 500) -> httpx.HTTPStatusError:
    """Create a mock HTTPStatusError for testing Descope API failures."""
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/test")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"{status_code} Server Error", request=request, response=response)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


CANONICAL_USER = {
    "id": "canonical-user-1",
    "email": "test@example.com",
    "user_name": "test",
    "given_name": "Test",
    "family_name": "User",
    "status": "active",
}


@pytest.fixture
def resolver() -> AsyncMock:
    """Stands in for IdentityResolutionService: the caller is the canonical ``CANONICAL_USER``."""
    resolver = AsyncMock()
    resolver.resolve.return_value = Ok({"user": CANONICAL_USER})
    return resolver


@pytest.fixture
async def client(resolver):
    async def _session():
        yield MagicMock()

    app.dependency_overrides[get_identity_resolution_service] = lambda: resolver
    app.dependency_overrides[get_async_session] = _session
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_identity_resolution_service, None)
        app.dependency_overrides.pop(get_async_session, None)


ADMIN_CLAIMS = {
    "sub": "user123",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["admin"], "permissions": ["settings.manage"]},
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["viewer"], "permissions": ["projects.read"]},
    },
}

NO_TENANT_CLAIMS = {
    "sub": "user789",
    "tenants": {},
}


@pytest.mark.anyio
async def test_profile_rejects_unauthenticated(client):
    response = await client.get("/api/profile")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_user.return_value = {
        "name": "Test User",
        "email": "test@example.com",
        "customAttributes": {"department": "Engineering", "job_title": "Developer"},
    }
    app.state.descope_client = mock_client

    response = await client.get("/api/profile", headers={"Authorization": "Bearer valid.token"})

    assert (response.status_code, response.json()) == (
        200,
        {
            "user_id": "user123",
            "name": "Test User",
            "email": "test@example.com",
            "custom_attributes": {"department": "Engineering", "job_title": "Developer"},
        },
    )
    mock_client.load_user.assert_awaited_once_with("user123")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_serves_only_the_attributes_the_api_allows(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_user.return_value = {"customAttributes": {"department": "Engineering", "internal": "x"}}
    app.state.descope_client = mock_client

    response = await client.get("/api/profile", headers={"Authorization": "Bearer valid.token"})

    assert response.json()["custom_attributes"] == {"department": "Engineering"}


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_without_a_canonical_user_is_blank(mock_validate, client, resolver):
    mock_validate.return_value = ADMIN_CLAIMS
    resolver.resolve.return_value = Error(NotFound(message="No identity"))
    mock_client = AsyncMock()
    mock_client.load_user.return_value = {"customAttributes": {}}
    app.state.descope_client = mock_client

    response = await client.get("/api/profile", headers={"Authorization": "Bearer valid.token"})

    assert (response.status_code, response.json()) == (
        200,
        {"user_id": "user123", "name": "", "email": "", "custom_attributes": {}},
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_degrades_when_the_canonical_user_cannot_be_read(mock_validate, client, resolver):
    """A database or cache failure leaves name and email blank: the profile is informational."""
    mock_validate.return_value = ADMIN_CLAIMS
    resolver.resolve.side_effect = RuntimeError("database unavailable")
    mock_client = AsyncMock()
    mock_client.load_user.return_value = {"customAttributes": {"department": "Engineering"}}
    app.state.descope_client = mock_client

    response = await client.get("/api/profile", headers={"Authorization": "Bearer valid.token"})

    assert (response.status_code, response.json()) == (
        200,
        {"user_id": "user123", "name": "", "email": "", "custom_attributes": {"department": "Engineering"}},
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_handles_api_failure(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_user.side_effect = _make_http_status_error(500)
    app.state.descope_client = mock_client

    response = await client.get("/api/profile", headers={"Authorization": "Bearer valid.token"})
    # Profile degrades gracefully on Descope API errors — returns 200 with the
    # canonical name and email and no custom attributes, since profile is informational.
    assert (response.status_code, response.json()) == (
        200,
        {"user_id": "user123", "name": "Test User", "email": "test@example.com", "custom_attributes": {}},
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_attribute(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.resolve_login_id.return_value = "user123@example.com"
    app.state.descope_client = mock_client

    response = await client.patch(
        "/api/profile",
        headers={"Authorization": "Bearer valid.token"},
        json={"key": "department", "value": "Product"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    mock_client.resolve_login_id.assert_called_once_with("user123")
    mock_client.update_user_custom_attribute.assert_called_once_with("user123@example.com", "department", "Product")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_tenant_settings(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_tenant.return_value = {
        "name": "Acme Corp",
        "customAttributes": {"plan_tier": "pro", "max_members": 50},
    }
    app.state.descope_client = mock_client

    response = await client.get("/api/tenants/current/settings", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == "tenant-abc"
    assert data["custom_attributes"]["plan_tier"] == "pro"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_tenant_settings_403_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.get("/api/tenants/current/settings", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_as_admin(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    app.state.descope_client = mock_client

    response = await client.patch(
        "/api/tenants/current/settings",
        headers={"Authorization": "Bearer valid.token"},
        json={"custom_attributes": {"plan_tier": "enterprise", "max_members": 200}},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    mock_client.update_tenant_custom_attributes.assert_called_once_with(
        "tenant-abc", {"plan_tier": "enterprise", "max_members": 200}
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.patch(
        "/api/tenants/current/settings",
        headers={"Authorization": "Bearer valid.token"},
        json={"custom_attributes": {"plan_tier": "enterprise"}},
    )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_update_profile_rejects_unauthenticated(client):
    response = await client.patch("/api/profile", json={"key": "department", "value": "Eng"})
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_tenant_settings_handles_api_failure(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.load_tenant.side_effect = _make_http_status_error(500)
    app.state.descope_client = mock_client

    response = await client.get("/api/tenants/current/settings", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 502
    assert "identity provider" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_rejects_disallowed_key(mock_validate, client):
    """Should reject attribute keys not in the allowlist."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.patch(
        "/api/profile",
        headers={"Authorization": "Bearer valid.token"},
        json={"key": "admin_override", "value": "true"},
    )
    assert response.status_code == 400
    assert "not allowed" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_rejects_missing_sub(mock_validate, client):
    """Should return 400 if the JWT has no sub claim."""
    mock_validate.return_value = {"email": "test@example.com", "dct": "tenant-abc", "tenants": {}}
    response = await client.patch(
        "/api/profile",
        headers={"Authorization": "Bearer valid.token"},
        json={"key": "department", "value": "Eng"},
    )
    assert response.status_code == 400
    assert "sub" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_handles_api_failure(mock_validate, client):
    """M3: update_profile_attribute should return 502 on Descope API error."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.resolve_login_id.return_value = "user123@example.com"
    mock_client.update_user_custom_attribute.side_effect = _make_http_status_error(500)
    app.state.descope_client = mock_client

    response = await client.patch(
        "/api/profile",
        headers={"Authorization": "Bearer valid.token"},
        json={"key": "department", "value": "Eng"},
    )
    assert response.status_code == 502
    assert "identity provider" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_rejects_disallowed_keys(mock_validate, client):
    """M1: tenant settings should reject attribute keys not in the allowlist."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.patch(
        "/api/tenants/current/settings",
        headers={"Authorization": "Bearer valid.token"},
        json={"custom_attributes": {"evil_key": "pwned"}},
    )
    assert response.status_code == 400
    assert "not allowed" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_rejects_missing_sub(mock_validate, client):
    """S1: get_profile should return 401 when sub claim is missing."""
    mock_validate.return_value = {"email": "test@example.com", "dct": "tenant-abc", "tenants": {}}
    response = await client.get("/api/profile", headers={"Authorization": "Bearer valid.token"})
    assert response.status_code == 401
    assert "sub" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_handles_api_failure(mock_validate, client):
    """Tenant settings update should return 502 on Descope API error."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.update_tenant_custom_attributes.side_effect = _make_http_status_error(500)
    app.state.descope_client = mock_client

    response = await client.patch(
        "/api/tenants/current/settings",
        headers={"Authorization": "Bearer valid.token"},
        json={"custom_attributes": {"plan_tier": "pro"}},
    )
    assert response.status_code == 502
    assert "identity provider" in response.json()["detail"]
