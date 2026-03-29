"""Unit tests for the FGA admin router endpoints."""

from unittest.mock import AsyncMock, patch

import httpx
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
    # Reset rate limiter storage between tests to avoid 429 interference
    from app.middleware.rate_limit import limiter

    limiter.reset()


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
            "permissions": ["projects.create"],
        },
    },
}

OWNER_CLAIMS = {
    "sub": "owner1",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {
            "roles": ["owner"],
            "permissions": [],
        },
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {
            "roles": ["viewer"],
            "permissions": ["projects.read"],
        },
    },
}

OTHER_TENANT_CLAIMS = {
    "sub": "user999",
    "dct": "tenant-xyz",
    "tenants": {
        "tenant-xyz": {
            "roles": ["admin"],
            "permissions": ["projects.create"],
        },
    },
}

NO_TENANT_CLAIMS = {
    "sub": "user789",
    "tenants": {},
}

AUTH_HEADER = {"Authorization": "Bearer valid.token"}


def _make_http_status_error(status_code: int = 500) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/authz")
    response = httpx.Response(status_code, request=request, text="error detail")
    return httpx.HTTPStatusError(f"{status_code} Server Error", request=request, response=response)


def _make_request_error() -> httpx.RequestError:
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/authz")
    return httpx.RequestError("Connection refused", request=request)


# --- Auth enforcement (403 for non-admin on all endpoints) ---


@pytest.mark.anyio
async def test_get_schema_rejects_unauthenticated(client):
    response = await client.get("/api/fga/schema")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_schema_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get("/api/fga/schema", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_put_schema_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.put("/api/fga/schema", headers=AUTH_HEADER, json={"schema": "v1"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_relation_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/fga/relations",
        headers=AUTH_HEADER,
        json={"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_relation_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.request(
        "DELETE",
        "/api/fga/relations",
        headers=AUTH_HEADER,
        json={"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_relations_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get(
        "/api/fga/relations", headers=AUTH_HEADER, params={"resource_type": "doc", "resource_id": "1"}
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_permission_rejects_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/fga/check",
        headers=AUTH_HEADER,
        json={"resource_type": "doc", "resource_id": "1", "relation": "viewer", "target": "u1"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_schema_rejects_no_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.get("/api/fga/schema", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_relation_rejects_no_tenant(mock_validate, client):
    """Relation endpoints require tenant context (dct claim)."""
    mock_validate.return_value = NO_TENANT_CLAIMS
    response = await client.post(
        "/api/fga/relations",
        headers=AUTH_HEADER,
        json={"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"},
    )
    assert response.status_code == 403


# --- GET /api/fga/schema ---


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_schema_success(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.get_fga_schema.return_value = {"schema": "type document {}"}
    mock_factory.return_value = mock_client

    response = await client.get("/api/fga/schema", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"schema": "type document {}"}
    mock_client.get_fga_schema.assert_called_once()


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_schema_empty(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.get_fga_schema.return_value = {}
    mock_factory.return_value = mock_client

    response = await client.get("/api/fga/schema", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"schema": ""}


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_schema_none_result(mock_validate, mock_factory, client):
    """get_fga_schema() returns None -> empty schema, not crash."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.get_fga_schema.return_value = None
    mock_factory.return_value = mock_client

    response = await client.get("/api/fga/schema", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"schema": ""}


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_schema_null_schema_value(mock_validate, mock_factory, client):
    """Schema key exists but value is None -> empty string."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.get_fga_schema.return_value = {"schema": None}
    mock_factory.return_value = mock_client

    response = await client.get("/api/fga/schema", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"schema": ""}


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_schema_owner_allowed(mock_validate, mock_factory, client):
    mock_validate.return_value = OWNER_CLAIMS
    mock_client = AsyncMock()
    mock_client.get_fga_schema.return_value = {"schema": "v1"}
    mock_factory.return_value = mock_client

    response = await client.get("/api/fga/schema", headers=AUTH_HEADER)
    assert response.status_code == 200


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_schema_descope_http_error(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.get_fga_schema.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.get("/api/fga/schema", headers=AUTH_HEADER)
    assert response.status_code == 502
    assert "error detail" not in response.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_schema_descope_400(mock_validate, mock_factory, client):
    """GET schema: Descope 400 -> HTTP 400 (not 502)."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.get_fga_schema.side_effect = _make_http_status_error(400)
    mock_factory.return_value = mock_client

    response = await client.get("/api/fga/schema", headers=AUTH_HEADER)
    assert response.status_code == 400


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_schema_network_error(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.get_fga_schema.side_effect = _make_request_error()
    mock_factory.return_value = mock_client

    response = await client.get("/api/fga/schema", headers=AUTH_HEADER)
    assert response.status_code == 502
    assert "Connection refused" not in response.json()["detail"]


# --- PUT /api/fga/schema ---


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_schema_success(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.get_fga_schema.return_value = {"schema": "type doc { relation viewer: user }"}
    mock_factory.return_value = mock_client

    response = await client.put(
        "/api/fga/schema", headers=AUTH_HEADER, json={"schema": "type doc { relation viewer: user }"}
    )
    assert response.status_code == 200
    assert response.json()["schema"] == "type doc { relation viewer: user }"
    mock_client.update_fga_schema.assert_called_once_with("type doc { relation viewer: user }")
    mock_client.get_fga_schema.assert_called_once()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_schema_empty_body_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.put("/api/fga/schema", headers=AUTH_HEADER, json={"schema": ""})
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_schema_readback_failure_returns_submitted(mock_validate, mock_factory, client):
    """Update succeeds but read-back fails -> return the submitted schema."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.get_fga_schema.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.put("/api/fga/schema", headers=AUTH_HEADER, json={"schema": "type doc {}"})
    assert response.status_code == 200
    assert response.json()["schema"] == "type doc {}"


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_schema_descope_400(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.update_fga_schema.side_effect = _make_http_status_error(400)
    mock_factory.return_value = mock_client

    response = await client.put("/api/fga/schema", headers=AUTH_HEADER, json={"schema": "bad schema"})
    assert response.status_code == 400


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_schema_descope_500(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.update_fga_schema.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.put("/api/fga/schema", headers=AUTH_HEADER, json={"schema": "valid"})
    assert response.status_code == 502
    assert "error detail" not in response.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_schema_network_error(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.update_fga_schema.side_effect = _make_request_error()
    mock_factory.return_value = mock_client

    response = await client.put("/api/fga/schema", headers=AUTH_HEADER, json={"schema": "valid"})
    assert response.status_code == 502
    assert "Connection refused" not in response.json()["detail"]


# --- POST /api/fga/relations ---


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_relation_success(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    body = {"resource_type": "document", "resource_id": "doc-1", "relation": "owner", "target": "user:u1"}
    response = await client.post("/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 201
    data = response.json()
    assert data["resource_type"] == "document"
    assert data["resource_id"] == "doc-1"
    assert data["relation"] == "owner"
    assert data["target"] == "user:u1"
    # Verify service call uses tenant-prefixed resource_id
    mock_client.create_relation.assert_called_once_with("document", "tenant-abc:doc-1", "owner", "user:u1")


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_relation_prefixes_tenant_id(mock_validate, mock_factory, client):
    """Verify resource_id is prefixed with tenant_id in the service call."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    body = {"resource_type": "project", "resource_id": "proj-42", "relation": "editor", "target": "user:u2"}
    response = await client.post("/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 201
    # The service layer receives the tenant-prefixed ID
    mock_client.create_relation.assert_called_once_with("project", "tenant-abc:proj-42", "editor", "user:u2")
    # The response returns the original (unprefixed) resource_id
    assert response.json()["resource_id"] == "proj-42"


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_relation_different_tenant_gets_different_prefix(mock_validate, mock_factory, client):
    """Different tenants get different resource_id prefixes, preventing cross-tenant access."""
    mock_validate.return_value = OTHER_TENANT_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "doc-1", "relation": "owner", "target": "user:u1"}
    response = await client.post("/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 201
    # tenant-xyz prefix, not tenant-abc
    mock_client.create_relation.assert_called_once_with("doc", "tenant-xyz:doc-1", "owner", "user:u1")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_relation_empty_fields_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    body = {"resource_type": "", "resource_id": "doc-1", "relation": "owner", "target": "u1"}
    response = await client.post("/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_relation_missing_field_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    body = {"resource_type": "doc", "resource_id": "1"}
    response = await client.post("/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_relation_descope_400(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_relation.side_effect = _make_http_status_error(400)
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    response = await client.post("/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 400


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_relation_descope_500(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_relation.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    response = await client.post("/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 502
    assert "error detail" not in response.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_relation_network_error(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_relation.side_effect = _make_request_error()
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    response = await client.post("/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 502


# --- DELETE /api/fga/relations ---


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_relation_success(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    response = await client.request("DELETE", "/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    # Verify tenant-prefixed resource_id in service call
    mock_client.delete_relation.assert_called_once_with("doc", "tenant-abc:1", "owner", "u1")


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_relation_prefixes_tenant_id(mock_validate, mock_factory, client):
    """Verify delete uses tenant-prefixed resource_id."""
    mock_validate.return_value = OTHER_TENANT_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "doc-5", "relation": "owner", "target": "u1"}
    response = await client.request("DELETE", "/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 200
    mock_client.delete_relation.assert_called_once_with("doc", "tenant-xyz:doc-5", "owner", "u1")


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_relation_descope_400(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.delete_relation.side_effect = _make_http_status_error(400)
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    response = await client.request("DELETE", "/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 400


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_relation_descope_500(mock_validate, mock_factory, client):
    """DELETE 500 -> 502 with opaque message."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.delete_relation.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    response = await client.request("DELETE", "/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 502
    assert "error detail" not in response.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_relation_network_error(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.delete_relation.side_effect = _make_request_error()
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    response = await client.request("DELETE", "/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 502


# --- GET /api/fga/relations ---


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_relations_success(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_relations.return_value = [
        {"relationDefinition": "owner", "target": "user:u1"},
        {"relationDefinition": "viewer", "target": "user:u2"},
    ]
    mock_factory.return_value = mock_client

    response = await client.get(
        "/api/fga/relations", headers=AUTH_HEADER, params={"resource_type": "doc", "resource_id": "1"}
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["relations"]) == 2
    # Verify service call uses tenant-prefixed resource_id
    mock_client.list_relations.assert_called_once_with("doc", "tenant-abc:1")


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_relations_strips_tenant_prefix(mock_validate, mock_factory, client):
    """Verify tenant prefix is stripped from resource_id in response."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_relations.return_value = [
        {"relationDefinition": "owner", "target": "user:u1", "resource_id": "tenant-abc:doc-1"},
        {"relationDefinition": "viewer", "target": "user:u2", "resource": "tenant-abc:doc-1"},
    ]
    mock_factory.return_value = mock_client

    response = await client.get(
        "/api/fga/relations", headers=AUTH_HEADER, params={"resource_type": "doc", "resource_id": "doc-1"}
    )
    assert response.status_code == 200
    relations = response.json()["relations"]
    assert relations[0]["resource_id"] == "doc-1"
    assert relations[1]["resource"] == "doc-1"


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_relations_empty(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_relations.return_value = []
    mock_factory.return_value = mock_client

    response = await client.get(
        "/api/fga/relations", headers=AUTH_HEADER, params={"resource_type": "doc", "resource_id": "1"}
    )
    assert response.status_code == 200
    assert response.json()["relations"] == []


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_relations_none_result(mock_validate, mock_factory, client):
    """list_relations() returns None -> empty list, not null."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_relations.return_value = None
    mock_factory.return_value = mock_client

    response = await client.get(
        "/api/fga/relations", headers=AUTH_HEADER, params={"resource_type": "doc", "resource_id": "1"}
    )
    assert response.status_code == 200
    assert response.json()["relations"] == []


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_relations_missing_params(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/fga/relations", headers=AUTH_HEADER)
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_relations_descope_error(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_relations.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    response = await client.get(
        "/api/fga/relations", headers=AUTH_HEADER, params={"resource_type": "doc", "resource_id": "1"}
    )
    assert response.status_code == 502
    assert "error detail" not in response.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_relations_descope_400(mock_validate, mock_factory, client):
    """GET relations: Descope 400 -> HTTP 400 (not 502)."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_relations.side_effect = _make_http_status_error(400)
    mock_factory.return_value = mock_client

    response = await client.get(
        "/api/fga/relations", headers=AUTH_HEADER, params={"resource_type": "doc", "resource_id": "1"}
    )
    assert response.status_code == 400


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_relations_network_error(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_relations.side_effect = _make_request_error()
    mock_factory.return_value = mock_client

    response = await client.get(
        "/api/fga/relations", headers=AUTH_HEADER, params={"resource_type": "doc", "resource_id": "1"}
    )
    assert response.status_code == 502


# --- POST /api/fga/check ---


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_permission_allowed(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "viewer", "target": "user:u1"}
    response = await client.post("/api/fga/check", headers=AUTH_HEADER, json=body)
    assert response.status_code == 200
    assert response.json()["allowed"] is True
    # Verify service call uses tenant-prefixed resource_id
    mock_client.check_permission.assert_called_once_with("doc", "tenant-abc:1", "viewer", "user:u1")


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_permission_denied(mock_validate, mock_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.return_value = False
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "user:u2"}
    response = await client.post("/api/fga/check", headers=AUTH_HEADER, json=body)
    assert response.status_code == 200
    assert response.json()["allowed"] is False


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_permission_none_returns_false(mock_validate, mock_factory, client):
    """check_permission() returns None -> allowed=False via bool()."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.return_value = None
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "viewer", "target": "user:u1"}
    response = await client.post("/api/fga/check", headers=AUTH_HEADER, json=body)
    assert response.status_code == 200
    assert response.json()["allowed"] is False


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_permission_prefixes_tenant_id(mock_validate, mock_factory, client):
    """Verify check_permission uses tenant-prefixed resource_id."""
    mock_validate.return_value = OTHER_TENANT_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "doc-1", "relation": "viewer", "target": "user:u1"}
    response = await client.post("/api/fga/check", headers=AUTH_HEADER, json=body)
    assert response.status_code == 200
    mock_client.check_permission.assert_called_once_with("doc", "tenant-xyz:doc-1", "viewer", "user:u1")


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_permission_descope_400(mock_validate, mock_factory, client):
    """POST check: Descope 400 -> HTTP 400 (not 502)."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.side_effect = _make_http_status_error(400)
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "viewer", "target": "user:u1"}
    response = await client.post("/api/fga/check", headers=AUTH_HEADER, json=body)
    assert response.status_code == 400
    assert "Validation error" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_permission_descope_error_returns_502(mock_validate, mock_factory, client):
    """FGA check must fail-closed: Descope API error -> 502, never fail-open."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.side_effect = _make_http_status_error(500)
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "viewer", "target": "user:u1"}
    response = await client.post("/api/fga/check", headers=AUTH_HEADER, json=body)
    assert response.status_code == 502
    assert "allowed" not in response.json()


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_permission_network_error_returns_502(mock_validate, mock_factory, client):
    """FGA check must fail-closed: network error -> 502, never fail-open."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.side_effect = _make_request_error()
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "viewer", "target": "user:u1"}
    response = await client.post("/api/fga/check", headers=AUTH_HEADER, json=body)
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_check_permission_missing_fields(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    body = {"resource_type": "doc", "resource_id": "1"}
    response = await client.post("/api/fga/check", headers=AUTH_HEADER, json=body)
    assert response.status_code == 422


# --- Owner role tests (owner should have same access as admin) ---


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_relation_owner_allowed(mock_validate, mock_factory, client):
    mock_validate.return_value = OWNER_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    response = await client.post("/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 201


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_relation_owner_allowed(mock_validate, mock_factory, client):
    mock_validate.return_value = OWNER_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    response = await client.request("DELETE", "/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert response.status_code == 200


# --- Sanitized error detail tests ---


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_400_error_detail_is_sanitized(mock_validate, mock_factory, client):
    """400 responses should wrap error text with 'Validation error:' prefix."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/authz")
    response = httpx.Response(400, request=request, text='{"message": "invalid resource type"}')
    mock_client.create_relation.side_effect = httpx.HTTPStatusError("400", request=request, response=response)
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    resp = await client.post("/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail.startswith("Validation error:")
    assert "invalid resource type" in detail


@pytest.mark.anyio
@patch("app.routers.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_400_error_detail_non_json_sanitized(mock_validate, mock_factory, client):
    """400 responses with non-JSON body are still wrapped safely."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/authz")
    response = httpx.Response(400, request=request, text="plain text error from descope")
    mock_client.create_relation.side_effect = httpx.HTTPStatusError("400", request=request, response=response)
    mock_factory.return_value = mock_client

    body = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    resp = await client.post("/api/fga/relations", headers=AUTH_HEADER, json=body)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail.startswith("Validation error:")
    assert "plain text error" in detail


# --- Helper function unit tests ---


def test_prefix_resource_id():
    from app.routers.fga import _prefix_resource_id

    assert _prefix_resource_id("tenant-abc", "doc-1") == "tenant-abc:doc-1"
    assert _prefix_resource_id("t1", "x") == "t1:x"


def test_strip_tenant_prefix():
    from app.routers.fga import _strip_tenant_prefix

    assert _strip_tenant_prefix("tenant-abc", "tenant-abc:doc-1") == "doc-1"
    assert _strip_tenant_prefix("tenant-abc", "other-tenant:doc-1") == "other-tenant:doc-1"
    assert _strip_tenant_prefix("tenant-abc", "doc-1") == "doc-1"


def test_sanitize_error_detail_json():
    from app.routers.fga import _sanitize_error_detail

    assert _sanitize_error_detail('{"message": "bad input"}') == "Validation error: bad input"


def test_sanitize_error_detail_plain_text():
    from app.routers.fga import _sanitize_error_detail

    result = _sanitize_error_detail("some raw error text")
    assert result.startswith("Validation error:")
    assert "some raw error text" in result


def test_sanitize_error_detail_truncates():
    from app.routers.fga import _sanitize_error_detail

    long_text = "x" * 500
    result = _sanitize_error_detail(long_text)
    # Should be truncated to 200 chars + "Validation error: " prefix
    assert len(result) <= 220
