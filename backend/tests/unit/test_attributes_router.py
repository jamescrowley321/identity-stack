"""Unit tests for the attributes router (user profile + tenant settings)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_identity_resolution_service, get_user_service
from app.errors.identity import NotFound
from app.main import app
from app.models.database import get_async_session
from app.services.adapters.base import PROVIDER_SYNC, PROVIDER_SYNC_FAILED
from app.services.user import UserService


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
    "id": "0b1d0b1d-0000-4000-8000-000000000001",
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
def users() -> AsyncMock:
    """Stands in for UserService: the caller's names are saved as sent."""
    users = AsyncMock(spec=UserService)

    async def update_own_names(*, user_id, given_name, family_name, subject):
        return Ok({**CANONICAL_USER, "given_name": given_name, "family_name": family_name})

    users.update_own_names.side_effect = update_own_names
    return users


@pytest.fixture
async def client(resolver, users):
    async def _session():
        yield MagicMock()

    app.dependency_overrides[get_identity_resolution_service] = lambda: resolver
    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_user_service] = lambda: users
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_identity_resolution_service, None)
        app.dependency_overrides.pop(get_async_session, None)
        app.dependency_overrides.pop(get_user_service, None)


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


BEARER = {"Authorization": "Bearer valid.token"}
UPDATED = {"status": "updated"}


def _profile(**overrides) -> dict:
    """The profile ``CANONICAL_USER`` gets, as the Descope principal ``user123``."""
    return {
        "user_id": "user123",
        "name": "Test User",
        "given_name": "Test",
        "family_name": "User",
        "email": "test@example.com",
        "custom_attributes": {},
        **overrides,
    }


def _descope(**methods) -> AsyncMock:
    mock_client = AsyncMock()
    for name, behaviour in methods.items():
        method = getattr(mock_client, name)
        if isinstance(behaviour, BaseException):
            method.side_effect = behaviour
        else:
            method.return_value = behaviour
    app.state.descope_client = mock_client
    return mock_client


@pytest.mark.anyio
async def test_profile_rejects_unauthenticated(client):
    response = await client.get("/api/profile")

    assert (response.status_code, response.json()) == (401, {"detail": "Missing or invalid authorization header"})


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile(mock_validate, client, resolver):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = _descope(load_user={"customAttributes": {"department": "Engineering", "job_title": "Developer"}})

    response = await client.get("/api/profile", headers=BEARER)

    assert (response.status_code, response.json()) == (
        200,
        _profile(custom_attributes={"department": "Engineering", "job_title": "Developer"}),
    )
    mock_client.load_user.assert_awaited_once_with("user123")
    resolver.resolve.assert_awaited_once_with(provider="descope", sub="user123")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_serves_only_the_attributes_the_api_allows(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    _descope(load_user={"customAttributes": {"department": "Engineering", "internal": "x"}})

    response = await client.get("/api/profile", headers=BEARER)

    assert (response.status_code, response.json()) == (200, _profile(custom_attributes={"department": "Engineering"}))


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_without_a_canonical_user_is_blank(mock_validate, client, resolver):
    mock_validate.return_value = ADMIN_CLAIMS
    resolver.resolve.return_value = Error(NotFound(message="No identity"))
    _descope(load_user={"customAttributes": {}})

    response = await client.get("/api/profile", headers=BEARER)

    assert (response.status_code, response.json()) == (
        200,
        _profile(name="", given_name="", family_name="", email=""),
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_degrades_when_the_canonical_user_cannot_be_read(mock_validate, client, resolver):
    """A database or cache failure leaves names and email blank: the profile is informational."""
    mock_validate.return_value = ADMIN_CLAIMS
    resolver.resolve.side_effect = RuntimeError("database unavailable")
    _descope(load_user={"customAttributes": {"department": "Engineering"}})

    response = await client.get("/api/profile", headers=BEARER)

    assert (response.status_code, response.json()) == (
        200,
        _profile(name="", given_name="", family_name="", email="", custom_attributes={"department": "Engineering"}),
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_handles_api_failure(mock_validate, client):
    """A Descope error leaves the custom attributes empty: the profile is informational."""
    mock_validate.return_value = ADMIN_CLAIMS
    _descope(load_user=_make_http_status_error(500))

    response = await client.get("/api/profile", headers=BEARER)

    assert (response.status_code, response.json()) == (200, _profile())


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_profile_rejects_missing_sub(mock_validate, client):
    mock_validate.return_value = {"email": "test@example.com", "dct": "tenant-abc", "tenants": {}}

    response = await client.get("/api/profile", headers=BEARER)

    assert (response.status_code, response.json()) == (401, {"detail": "Missing user identity (sub claim)"})


# --- Names -------------------------------------------------------------------------


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_saves_the_callers_names_and_pushes_them_to_their_own_account(
    mock_validate, client, users
):
    """A Descope principal of a Descope deployment: the names go to its own Descope user."""
    mock_validate.return_value = ADMIN_CLAIMS

    response = await client.patch(
        "/api/profile", headers=BEARER, json={"given_name": " Renamed ", "family_name": "Person"}
    )

    assert (response.status_code, response.json()) == (
        200,
        {**UPDATED, "name": "Renamed Person", "given_name": "Renamed", "family_name": "Person"},
    )
    users.update_own_names.assert_awaited_once_with(
        user_id=uuid.UUID(CANONICAL_USER["id"]), given_name="Renamed", family_name="Person", subject="user123"
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_tells_a_failed_push(mock_validate, client, users):
    mock_validate.return_value = ADMIN_CLAIMS
    users.update_own_names.side_effect = None
    users.update_own_names.return_value = Ok({**CANONICAL_USER, PROVIDER_SYNC: PROVIDER_SYNC_FAILED})

    response = await client.patch("/api/profile", headers=BEARER, json={"given_name": "Test", "family_name": "User"})

    assert (response.status_code, response.json()) == (
        200,
        {**UPDATED, "name": "Test User", "given_name": "Test", "family_name": "User", PROVIDER_SYNC: "failed"},
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_without_a_canonical_user_is_404(mock_validate, client, resolver, users):
    mock_validate.return_value = ADMIN_CLAIMS
    resolver.resolve.return_value = Error(NotFound(message="No identity"))

    response = await client.patch("/api/profile", headers=BEARER, json={"given_name": "Test", "family_name": "User"})

    assert (response.status_code, response.json()) == (404, {"detail": "Identity not found"})
    users.update_own_names.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"given_name": "   ", "family_name": "User"},
        {"given_name": "Test", "family_name": ""},
        {"given_name": "Test"},
        {"given_name": "x" * 256, "family_name": "User"},
        {"given_name": "Test", "family_name": "y" * 256},
        # 128 emoji are 256 UTF-16 code units, which is how Keycloak counts.
        {"given_name": "\U0001f600" * 128, "family_name": "User"},
        {"given_name": "Smith (Jr)", "family_name": "User"},
        {"given_name": "Test", "family_name": "A&B"},
        {"given_name": "Test", "family_name": "Back\\slash"},
        {"given_name": "Test", "family_name": "Tab\tbed"},
        {"given_name": "Test", "family_name": "User", "email": "new@example.com"},
        {"key": "department", "value": "Product"},
    ],
    ids=[
        "blank given name",
        "blank family name",
        "no family name",
        "long given name",
        "long family name",
        "emoji over 255 UTF-16 units",
        "parentheses",
        "ampersand",
        "backslash",
        "control character",
        "an email",
        "an attribute",
    ],
)
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_refuses_a_body_that_is_not_names(mock_validate, client, users, body):
    mock_validate.return_value = ADMIN_CLAIMS

    response = await client.patch("/api/profile", headers=BEARER, json=body)

    assert response.status_code == 422
    users.update_own_names.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("given_name", "family_name"),
    [("\U0001f600" * 127, "User"), ("Zoë", "O'Brien-Núñez"), ("x" * 255, "y" * 255)],
    ids=["emoji within 255 UTF-16 units", "accents and an apostrophe", "255 characters"],
)
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_takes_a_name_every_provider_takes(mock_validate, client, users, given_name, family_name):
    mock_validate.return_value = ADMIN_CLAIMS

    response = await client.patch(
        "/api/profile", headers=BEARER, json={"given_name": given_name, "family_name": family_name}
    )

    assert (response.status_code, response.json()) == (
        200,
        {**UPDATED, "name": f"{given_name} {family_name}", "given_name": given_name, "family_name": family_name},
    )


@pytest.mark.anyio
async def test_update_profile_rejects_unauthenticated(client):
    response = await client.patch("/api/profile", json={"given_name": "Test", "family_name": "User"})

    assert (response.status_code, response.json()) == (401, {"detail": "Missing or invalid authorization header"})


# --- Custom attributes -------------------------------------------------------------


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_attribute(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = _descope(resolve_login_id="user123@example.com")

    response = await client.patch(
        "/api/profile/attributes", headers=BEARER, json={"key": "department", "value": "Product"}
    )

    assert (response.status_code, response.json()) == (200, {**UPDATED, "key": "department", "value": "Product"})
    mock_client.resolve_login_id.assert_called_once_with("user123")
    mock_client.update_user_custom_attribute.assert_called_once_with("user123@example.com", "department", "Product")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_attribute_rejects_disallowed_key(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS

    response = await client.patch(
        "/api/profile/attributes", headers=BEARER, json={"key": "admin_override", "value": "true"}
    )

    assert (response.status_code, response.json()) == (400, {"detail": "Attribute 'admin_override' is not allowed"})


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_attribute_rejects_missing_sub(mock_validate, client):
    mock_validate.return_value = {"email": "test@example.com", "dct": "tenant-abc", "tenants": {}}

    response = await client.patch("/api/profile/attributes", headers=BEARER, json={"key": "department", "value": "Eng"})

    assert (response.status_code, response.json()) == (400, {"detail": "Missing user identity (sub claim)"})


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_attribute_handles_api_failure(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    _descope(resolve_login_id="user123@example.com", update_user_custom_attribute=_make_http_status_error(500))

    response = await client.patch("/api/profile/attributes", headers=BEARER, json={"key": "department", "value": "Eng"})

    assert (response.status_code, response.json()) == (
        502,
        {"detail": "Failed to update attribute in identity provider"},
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "value",
    ["NaN", "Infinity", "-Infinity", '"' + "x" * 2049 + '"'],
    ids=["nan", "infinity", "minus infinity", "long text"],
)
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_an_attribute_value_no_provider_holds_is_refused(mock_validate, client, value):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = _descope()
    content = f'{{"custom_attributes": {{"max_members": {value}}}}}'

    tenant = await client.patch(
        "/api/tenants/current/settings", headers={**BEARER, "Content-Type": "application/json"}, content=content
    )
    profile = await client.patch(
        "/api/profile/attributes",
        headers={**BEARER, "Content-Type": "application/json"},
        content=f'{{"key": "department", "value": {value}}}',
    )

    assert (tenant.status_code, profile.status_code) == (422, 422)
    assert mock_client.mock_calls == []


# --- Tenant settings ---------------------------------------------------------------


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_tenant_settings(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    _descope(load_tenant={"name": "Acme Corp", "customAttributes": {"plan_tier": "pro", "max_members": 50}})

    response = await client.get("/api/tenants/current/settings", headers=BEARER)

    assert (response.status_code, response.json()) == (
        200,
        {"tenant_id": "tenant-abc", "name": "Acme Corp", "custom_attributes": {"plan_tier": "pro", "max_members": 50}},
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_tenant_settings_403_without_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS

    response = await client.get("/api/tenants/current/settings", headers=BEARER)

    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_tenant_settings_handles_api_failure(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    _descope(load_tenant=_make_http_status_error(500))

    response = await client.get("/api/tenants/current/settings", headers=BEARER)

    assert (response.status_code, response.json()) == (
        502,
        {"detail": "Failed to load tenant settings from identity provider"},
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_as_admin(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = _descope()
    attributes = {"plan_tier": "enterprise", "max_members": 200}

    response = await client.patch(
        "/api/tenants/current/settings", headers=BEARER, json={"custom_attributes": attributes}
    )

    assert (response.status_code, response.json()) == (
        200,
        {**UPDATED, "tenant_id": "tenant-abc", "custom_attributes": attributes},
    )
    mock_client.update_tenant_custom_attributes.assert_called_once_with("tenant-abc", attributes)


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS

    response = await client.patch(
        "/api/tenants/current/settings", headers=BEARER, json={"custom_attributes": {"plan_tier": "enterprise"}}
    )

    assert (response.status_code, response.json()) == (403, {"detail": "Insufficient permissions"})


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_rejects_disallowed_keys(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS

    response = await client.patch(
        "/api/tenants/current/settings", headers=BEARER, json={"custom_attributes": {"evil_key": "pwned"}}
    )

    assert (response.status_code, response.json()) == (400, {"detail": "Attribute(s) not allowed: evil_key"})


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_handles_api_failure(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    _descope(update_tenant_custom_attributes=_make_http_status_error(500))

    response = await client.patch(
        "/api/tenants/current/settings", headers=BEARER, json={"custom_attributes": {"plan_tier": "pro"}}
    )

    assert (response.status_code, response.json()) == (
        502,
        {"detail": "Failed to update tenant settings in identity provider"},
    )
