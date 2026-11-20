"""Unit tests for the IdP links router (Story 4.2).

Tests cover:
- Auth enforcement: unauthenticated → 401, wrong role → 403
- Tenant scoping: user not in caller's tenant → 404
- Happy paths: list links, create link (201), delete link (204)
- Error handling: NotFound → 404, Conflict → 409
- Input validation: invalid UUID → 422, empty external_sub rejected, metadata limits

The caller's tenant is ``get_canonical_tenant_id``'s, overridden here with a
canonical tenant; how the resolver gives it to each provider's principal, and
refuses a Descope principal, is ``test_idp_links_canonical.py``'s (#722).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from expression import Error, Ok
from fastapi import Depends
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_idp_link_service
from app.dependencies.tenant import get_canonical_tenant_id
from app.errors.identity import Conflict, NotFound
from app.main import app
from app.models.database import get_async_session
from app.services.idp_link import IdPLinkService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
def mock_idp_link_service():
    return AsyncMock(spec=IdPLinkService)


@pytest.fixture
def mock_session():
    return AsyncMock()


@pytest.fixture(autouse=True)
def _override_services(mock_idp_link_service, mock_session):
    app.dependency_overrides[get_idp_link_service] = lambda: mock_idp_link_service
    app.dependency_overrides[get_async_session] = lambda: mock_session
    yield
    app.dependency_overrides.pop(get_idp_link_service, None)
    app.dependency_overrides.pop(get_async_session, None)


TENANT_ID = "51c5957b-684a-453f-8ab1-8f239999c4d8"


@pytest.fixture(autouse=True)
def _canonical_tenant():
    """The tenant resolver's canonical tenant for the caller (tests of a Descope principal drop it)."""
    app.dependency_overrides[get_canonical_tenant_id] = lambda: TENANT_ID
    yield
    app.dependency_overrides.pop(get_canonical_tenant_id, None)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


ADMIN_CLAIMS = {
    "sub": "admin1",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["admin"], "permissions": []},
    },
}

OWNER_CLAIMS = {
    "sub": "owner1",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["owner"], "permissions": []},
    },
}

VIEWER_CLAIMS = {
    "sub": "viewer1",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["viewer"], "permissions": []},
    },
}

OPERATOR_CLAIMS = {
    "sub": "operator1",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["operator"], "permissions": []},
    },
}

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

USER_ID = str(uuid.uuid4())
PROVIDER_ID = str(uuid.uuid4())
LINK_ID = str(uuid.uuid4())

SAMPLE_LINK = {
    "id": LINK_ID,
    "user_id": USER_ID,
    "provider_id": PROVIDER_ID,
    "external_sub": "ext-sub-123",
    "external_email": "ext@example.com",
    "metadata_": None,
}


def _mock_tenant_check_pass():
    """Patch UserTenantRoleRepository so _verify_user_in_tenant passes."""
    mock_repo_instance = AsyncMock()
    mock_repo_instance.list_by_user_tenant.return_value = [MagicMock()]  # non-empty → passes
    return patch("app.routers.idp_links.UserTenantRoleRepository", return_value=mock_repo_instance)


def _mock_tenant_check_fail():
    """Patch UserTenantRoleRepository so _require_user_in_tenant fails (user not in tenant)."""
    mock_repo_instance = AsyncMock()
    mock_repo_instance.list_by_user_tenant.return_value = []  # empty → 404
    return patch("app.routers.idp_links.UserTenantRoleRepository", return_value=mock_repo_instance)


# --- Auth enforcement ---


@pytest.mark.anyio
async def test_list_links_rejects_unauthenticated(client):
    response = await client.get(f"/api/users/{USER_ID}/idp-links")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_create_link_rejects_unauthenticated(client):
    response = await client.post(
        f"/api/users/{USER_ID}/idp-links",
        json={"provider_id": PROVIDER_ID, "external_sub": "ext-sub"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_delete_link_rejects_unauthenticated(client):
    response = await client.delete(f"/api/users/{USER_ID}/idp-links/{LINK_ID}")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_links_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get(f"/api/users/{USER_ID}/idp-links", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_link_rejected_for_operator(mock_validate, client):
    """Admin/owner-only endpoints reject operator role."""
    mock_validate.return_value = OPERATOR_CLAIMS
    response = await client.post(
        f"/api/users/{USER_ID}/idp-links",
        headers=AUTH_HEADER,
        json={"provider_id": PROVIDER_ID, "external_sub": "ext-sub"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_link_rejected_without_tenant(mock_validate, client):
    mock_validate.return_value = {"sub": "user1", "tenants": {}}
    response = await client.delete(
        f"/api/users/{USER_ID}/idp-links/{LINK_ID}",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 403


# --- Tenant scoping ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_links_user_not_in_tenant_returns_404(mock_validate, client):
    """User not in caller's tenant → 404 (does not leak user existence)."""
    mock_validate.return_value = ADMIN_CLAIMS
    with _mock_tenant_check_fail():
        response = await client.get(f"/api/users/{USER_ID}/idp-links", headers=AUTH_HEADER)
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_link_user_not_in_tenant_returns_404(mock_validate, client):
    """User not in caller's tenant → 404 on create."""
    mock_validate.return_value = ADMIN_CLAIMS
    with _mock_tenant_check_fail():
        response = await client.post(
            f"/api/users/{USER_ID}/idp-links",
            headers=AUTH_HEADER,
            json={"provider_id": PROVIDER_ID, "external_sub": "ext-sub"},
        )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_link_user_not_in_tenant_returns_404(mock_validate, client):
    """User not in caller's tenant → 404 on delete."""
    mock_validate.return_value = ADMIN_CLAIMS
    with _mock_tenant_check_fail():
        response = await client.delete(
            f"/api/users/{USER_ID}/idp-links/{LINK_ID}",
            headers=AUTH_HEADER,
        )
    assert response.status_code == 404


# --- Happy paths ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_user_idp_links(mock_validate, mock_idp_link_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    links = [SAMPLE_LINK, {**SAMPLE_LINK, "external_sub": "ext-sub-456"}]
    mock_idp_link_service.get_user_idp_links.return_value = Ok(links)

    with _mock_tenant_check_pass():
        response = await client.get(f"/api/users/{USER_ID}/idp-links", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert "idp_links" in data
    assert len(data["idp_links"]) == 2
    mock_idp_link_service.get_user_idp_links.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_user_idp_links_empty(mock_validate, mock_idp_link_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_idp_link_service.get_user_idp_links.return_value = Ok([])

    with _mock_tenant_check_pass():
        response = await client.get(f"/api/users/{USER_ID}/idp-links", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json()["idp_links"] == []


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_links_owner_allowed(mock_validate, mock_idp_link_service, client):
    """Owner role is also allowed for IdP link endpoints."""
    mock_validate.return_value = OWNER_CLAIMS
    mock_idp_link_service.get_user_idp_links.return_value = Ok([])

    with _mock_tenant_check_pass():
        response = await client.get(f"/api/users/{USER_ID}/idp-links", headers=AUTH_HEADER)
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_idp_link(mock_validate, mock_idp_link_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_idp_link_service.create_idp_link.return_value = Ok(SAMPLE_LINK)

    with _mock_tenant_check_pass():
        response = await client.post(
            f"/api/users/{USER_ID}/idp-links",
            headers=AUTH_HEADER,
            json={"provider_id": PROVIDER_ID, "external_sub": "ext-sub-123"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["external_sub"] == "ext-sub-123"
    mock_idp_link_service.create_idp_link.assert_awaited_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_idp_link_with_metadata(mock_validate, mock_idp_link_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    metadata = {"source": "migration"}
    mock_idp_link_service.create_idp_link.return_value = Ok({**SAMPLE_LINK, "metadata_": metadata})

    with _mock_tenant_check_pass():
        response = await client.post(
            f"/api/users/{USER_ID}/idp-links",
            headers=AUTH_HEADER,
            json={
                "provider_id": PROVIDER_ID,
                "external_sub": "ext-sub-123",
                "metadata": metadata,
            },
        )
    assert response.status_code == 201


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_idp_link(mock_validate, mock_idp_link_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_idp_link_service.delete_idp_link.return_value = Ok({"status": "deleted", "link_id": LINK_ID})

    with _mock_tenant_check_pass():
        response = await client.delete(
            f"/api/users/{USER_ID}/idp-links/{LINK_ID}",
            headers=AUTH_HEADER,
        )
    assert response.status_code == 204
    assert response.content == b""


# --- Error handling ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_link_user_not_found(mock_validate, mock_idp_link_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_idp_link_service.create_idp_link.return_value = Error(NotFound(message=f"User '{USER_ID}' not found"))

    with _mock_tenant_check_pass():
        response = await client.post(
            f"/api/users/{USER_ID}/idp-links",
            headers=AUTH_HEADER,
            json={"provider_id": PROVIDER_ID, "external_sub": "ext-sub"},
        )
    assert response.status_code == 404


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_link_conflict(mock_validate, mock_idp_link_service, client):
    """Duplicate user+provider → 409."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_idp_link_service.create_idp_link.return_value = Error(Conflict(message="IdP link already exists"))

    with _mock_tenant_check_pass():
        response = await client.post(
            f"/api/users/{USER_ID}/idp-links",
            headers=AUTH_HEADER,
            json={"provider_id": PROVIDER_ID, "external_sub": "ext-sub"},
        )
    assert response.status_code == 409


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_link_not_found(mock_validate, mock_idp_link_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_idp_link_service.delete_idp_link.return_value = Error(NotFound(message=f"IdP link '{LINK_ID}' not found"))

    with _mock_tenant_check_pass():
        response = await client.delete(
            f"/api/users/{USER_ID}/idp-links/{LINK_ID}",
            headers=AUTH_HEADER,
        )
    assert response.status_code == 404


# --- Input validation ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_links_invalid_user_uuid(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/users/not-a-uuid/idp-links", headers=AUTH_HEADER)
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_link_invalid_user_uuid(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/users/not-a-uuid/idp-links",
        headers=AUTH_HEADER,
        json={"provider_id": PROVIDER_ID, "external_sub": "ext-sub"},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_link_invalid_user_uuid(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.delete(
        "/api/users/not-a-uuid/idp-links/{LINK_ID}",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_link_invalid_link_uuid(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    with _mock_tenant_check_pass():
        response = await client.delete(
            f"/api/users/{USER_ID}/idp-links/not-a-uuid",
            headers=AUTH_HEADER,
        )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_link_empty_external_sub_rejected(mock_validate, client):
    """external_sub requires min_length=1."""
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        f"/api/users/{USER_ID}/idp-links",
        headers=AUTH_HEADER,
        json={"provider_id": PROVIDER_ID, "external_sub": ""},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_link_metadata_too_many_keys_rejected(mock_validate, client):
    """metadata with >20 keys is rejected."""
    mock_validate.return_value = ADMIN_CLAIMS
    metadata = {f"key{i}": f"val{i}" for i in range(21)}
    response = await client.post(
        f"/api/users/{USER_ID}/idp-links",
        headers=AUTH_HEADER,
        json={"provider_id": PROVIDER_ID, "external_sub": "ext-sub", "metadata": metadata},
    )
    assert response.status_code == 422


# --- A Descope principal has no canonical tenant (#722) ---

# Descope issues opaque tenant ids like "T3Bj8QOcyflY8V0bvSu1eEoHmjk6". Whatever
# its `dct`, it names no canonical tenant, so the guard answers 404, never 500.

DESCOPE_TENANT_ID = "T3Bj8QOcyflY8V0bvSu1eEoHmjk6"

DESCOPE_ADMIN_CLAIMS = {
    "sub": "admin1",
    "dct": DESCOPE_TENANT_ID,
    "tenants": {
        DESCOPE_TENANT_ID: {"roles": ["admin"], "permissions": []},
    },
}


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_links_non_uuid_tenant_returns_404_not_500(mock_validate, client):
    app.dependency_overrides.pop(get_canonical_tenant_id)
    """A Descope-shaped `dct` cannot match a canonical UUID FK → 404, never 500."""
    mock_validate.return_value = DESCOPE_ADMIN_CLAIMS
    response = await client.get(f"/api/users/{USER_ID}/idp-links", headers=AUTH_HEADER)
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "User not found in tenant"
    assert "type" in body, f"tenant-guard 404 must be an RFC 9457 problem detail, got {body}"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_link_non_uuid_tenant_returns_404_not_500(mock_validate, client):
    app.dependency_overrides.pop(get_canonical_tenant_id)
    """Create under a Descope-shaped `dct` → 404, never 500."""
    mock_validate.return_value = DESCOPE_ADMIN_CLAIMS
    response = await client.post(
        f"/api/users/{USER_ID}/idp-links",
        headers=AUTH_HEADER,
        json={"provider_id": PROVIDER_ID, "external_sub": "ext-sub"},
    )
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "User not found in tenant"
    assert "type" in body, f"tenant-guard 404 must be an RFC 9457 problem detail, got {body}"


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_link_non_uuid_tenant_returns_404_not_500(mock_validate, client):
    app.dependency_overrides.pop(get_canonical_tenant_id)
    """Delete under a Descope-shaped `dct` → 404, never 500."""
    mock_validate.return_value = DESCOPE_ADMIN_CLAIMS
    response = await client.delete(
        f"/api/users/{USER_ID}/idp-links/{LINK_ID}",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "User not found in tenant"
    assert "type" in body, f"tenant-guard 404 must be an RFC 9457 problem detail, got {body}"


# --- The guard is fail-closed by construction ---


@pytest.mark.anyio
async def test_guard_raises_rather_than_returning_a_response():
    """The point of the change: there is no return value a caller could drop.

    While it handed back ``Response | None``, a route that called the guard and
    ignored the result silently lost tenant isolation — no type error, no failing
    test. Raising removes the thing that can be forgotten, so assert on the
    mechanism and not merely on the 404 the existing tests already cover.
    """
    from app.errors.identity import IdentityErrorRaised
    from app.errors.identity import NotFound as NotFoundError
    from app.routers.idp_links import _require_user_in_tenant

    with pytest.raises(IdentityErrorRaised) as exc_info:
        await _require_user_in_tenant(uuid.UUID(USER_ID), None, AsyncMock())

    assert isinstance(exc_info.value.error, NotFoundError)


@pytest.mark.anyio
async def test_guard_returns_none_when_the_check_passes():
    """The success path must stay falsy-but-unused, not a response to propagate."""
    from app.routers.idp_links import _require_user_in_tenant

    with _mock_tenant_check_pass():
        assert await _require_user_in_tenant(uuid.UUID(USER_ID), TENANT_ID, AsyncMock()) is None


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_a_route_that_ignores_the_guard_still_refuses(mock_validate, client):
    """A future route that forgets to propagate must still be safe.

    This is the regression the issue is actually about. It mounts a route that
    calls the guard and discards whatever it produces — the exact mistake that
    used to defeat the check — and asserts the request is still refused.
    """
    from app.errors.identity import IdentityErrorRaised
    from app.errors.problem_detail import identity_error_raised_handler
    from app.routers.idp_links import _require_user_in_tenant

    @app.get("/api/__test_forgetful_route/{user_id}")
    async def _forgetful(user_id: str, tenant_id=Depends(get_canonical_tenant_id), session=Depends(get_async_session)):
        await _require_user_in_tenant(uuid.UUID(user_id), tenant_id, session)
        return {"leaked": "tenant isolation bypassed"}

    app.add_exception_handler(IdentityErrorRaised, identity_error_raised_handler)
    mock_validate.return_value = ADMIN_CLAIMS
    try:
        with _mock_tenant_check_fail():
            response = await client.get(f"/api/__test_forgetful_route/{USER_ID}", headers=AUTH_HEADER)
        assert response.status_code == 404
        assert "leaked" not in response.text
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) != "/api/__test_forgetful_route/{user_id}"
        ]
