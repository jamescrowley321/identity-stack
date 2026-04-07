"""Unit tests for the IdP links router (Story 4.2).

Tests cover:
- Auth enforcement: unauthenticated → 401, wrong role → 403
- Happy paths: list links, create link (201), delete link (204)
- Error handling: NotFound → 404, Conflict → 409
- Input validation: invalid UUID → 422, empty external_sub rejected
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_idp_link_service
from app.errors.identity import Conflict, NotFound
from app.main import app
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


@pytest.fixture(autouse=True)
def _override_services(mock_idp_link_service):
    app.dependency_overrides[get_idp_link_service] = lambda: mock_idp_link_service
    yield
    app.dependency_overrides.pop(get_idp_link_service, None)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


TENANT_ID = "51c5957b-684a-453f-8ab1-8f239999c4d8"

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


# --- Happy paths ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_user_idp_links(mock_validate, mock_idp_link_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    links = [SAMPLE_LINK, {**SAMPLE_LINK, "external_sub": "ext-sub-456"}]
    mock_idp_link_service.get_user_idp_links.return_value = Ok(links)

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

    response = await client.get(f"/api/users/{USER_ID}/idp-links", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json()["idp_links"] == []


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_links_owner_allowed(mock_validate, mock_idp_link_service, client):
    """Owner role is also allowed for IdP link endpoints."""
    mock_validate.return_value = OWNER_CLAIMS
    mock_idp_link_service.get_user_idp_links.return_value = Ok([])

    response = await client.get(f"/api/users/{USER_ID}/idp-links", headers=AUTH_HEADER)
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_idp_link(mock_validate, mock_idp_link_service, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_idp_link_service.create_idp_link.return_value = Ok(SAMPLE_LINK)

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
