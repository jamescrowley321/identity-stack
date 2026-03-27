"""Unit tests for the documents router with FGA-based access control."""

from unittest.mock import AsyncMock, patch

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


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


ADMIN_CLAIMS = {
    "sub": "user123",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["admin"], "permissions": []},
    },
}

OTHER_USER_CLAIMS = {
    "sub": "user456",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["member"], "permissions": []},
    },
}


@pytest.mark.anyio
@patch("app.routers.documents.get_fga_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_document(mock_validate, mock_fga_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_fga = AsyncMock()
    mock_fga_factory.return_value = mock_fga

    response = await client.post(
        "/api/documents",
        headers={"Authorization": "Bearer tok"},
        json={"title": "My Doc", "content": "Hello world"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "My Doc"
    assert body["content"] == "Hello world"
    assert body["created_by"] == "user123"
    assert body["tenant_id"] == "tenant-abc"
    # FGA owner relation created
    mock_fga.create_relation.assert_called_once_with("document", body["id"], "owner", "user123")


@pytest.mark.anyio
@patch("app.routers.documents.get_fga_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_documents(mock_validate, mock_fga_factory, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_fga = AsyncMock()
    mock_fga.list_user_resources.return_value = []
    mock_fga_factory.return_value = mock_fga

    response = await client.get("/api/documents", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    assert response.json()["documents"] == []


@pytest.mark.anyio
@patch("app.dependencies.fga.get_fga_client")
@patch("app.routers.documents.get_fga_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_document_allowed(mock_validate, mock_fga_factory, mock_fga_dep, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_fga = AsyncMock()
    mock_fga.create_relation.return_value = None
    mock_fga_factory.return_value = mock_fga
    # FGA dependency check passes
    mock_fga_dep_instance = AsyncMock()
    mock_fga_dep_instance.check_permission.return_value = True
    mock_fga_dep.return_value = mock_fga_dep_instance

    # First create a document
    create_resp = await client.post(
        "/api/documents",
        headers={"Authorization": "Bearer tok"},
        json={"title": "Test Doc"},
    )
    doc_id = create_resp.json()["id"]

    # Then get it
    response = await client.get(f"/api/documents/{doc_id}", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    assert response.json()["title"] == "Test Doc"


@pytest.mark.anyio
@patch("app.dependencies.fga.get_fga_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_document_denied(mock_validate, mock_fga_dep, client):
    mock_validate.return_value = OTHER_USER_CLAIMS
    mock_fga_dep_instance = AsyncMock()
    mock_fga_dep_instance.check_permission.return_value = False
    mock_fga_dep.return_value = mock_fga_dep_instance

    response = await client.get("/api/documents/nonexistent", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_fga_client")
@patch("app.routers.documents.get_fga_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document(mock_validate, mock_fga_factory, mock_fga_dep, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_fga = AsyncMock()
    mock_fga.create_relation.return_value = None
    mock_fga_factory.return_value = mock_fga
    mock_fga_dep_instance = AsyncMock()
    mock_fga_dep_instance.check_permission.return_value = True
    mock_fga_dep.return_value = mock_fga_dep_instance
    # Mock Descope client to validate target user is in the same tenant
    mock_descope_client = AsyncMock()
    mock_descope_client.load_user.return_value = {"userTenants": [{"tenantId": "tenant-abc"}]}
    mock_descope.return_value = mock_descope_client

    # Create document first
    create_resp = await client.post(
        "/api/documents",
        headers={"Authorization": "Bearer tok"},
        json={"title": "Shared Doc"},
    )
    doc_id = create_resp.json()["id"]

    # Share it
    response = await client.post(
        f"/api/documents/{doc_id}/share",
        headers={"Authorization": "Bearer tok"},
        json={"user_id": "user456", "relation": "viewer"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "shared"
    # Verify FGA relation created for sharing (the second create_relation call)
    share_call = mock_fga.create_relation.call_args_list[-1]
    assert share_call.args == ("document", doc_id, "viewer", "user456")


@pytest.mark.anyio
@patch("app.dependencies.fga.get_fga_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_invalid_relation(mock_validate, mock_fga_dep, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_fga_dep_instance = AsyncMock()
    mock_fga_dep_instance.check_permission.return_value = True
    mock_fga_dep.return_value = mock_fga_dep_instance

    response = await client.post(
        "/api/documents/doc1/share",
        headers={"Authorization": "Bearer tok"},
        json={"user_id": "user456", "relation": "admin"},
    )
    assert response.status_code == 422  # Pydantic validates Literal["viewer", "editor"]


@pytest.mark.anyio
@patch("app.dependencies.fga.get_fga_client")
@patch("app.routers.documents.get_fga_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_document(mock_validate, mock_fga_factory, mock_fga_dep, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_fga = AsyncMock()
    mock_fga.create_relation.return_value = None
    mock_fga.list_relations.return_value = [{"target": "user123", "relation": "owner"}]
    mock_fga.delete_relation.return_value = None
    mock_fga_factory.return_value = mock_fga
    mock_fga_dep_instance = AsyncMock()
    mock_fga_dep_instance.check_permission.return_value = True
    mock_fga_dep.return_value = mock_fga_dep_instance

    # Create then delete
    create_resp = await client.post(
        "/api/documents",
        headers={"Authorization": "Bearer tok"},
        json={"title": "To Delete"},
    )
    doc_id = create_resp.json()["id"]

    response = await client.delete(f"/api/documents/{doc_id}", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    assert response.json()["status"] == "deleted"


@pytest.mark.anyio
async def test_documents_reject_unauthenticated(client):
    response = await client.get("/api/documents")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.dependencies.fga.get_fga_client")
@patch("app.routers.documents.get_fga_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_revoke_share(mock_validate, mock_fga_factory, mock_fga_dep, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_fga = AsyncMock()
    mock_fga.create_relation.return_value = None
    mock_fga.delete_relation.return_value = None
    mock_fga_factory.return_value = mock_fga
    mock_fga_dep_instance = AsyncMock()
    mock_fga_dep_instance.check_permission.return_value = True
    mock_fga_dep.return_value = mock_fga_dep_instance

    # Create document
    create_resp = await client.post(
        "/api/documents",
        headers={"Authorization": "Bearer tok"},
        json={"title": "Revoke Test"},
    )
    doc_id = create_resp.json()["id"]

    response = await client.delete(
        f"/api/documents/{doc_id}/share/user456",
        headers={"Authorization": "Bearer tok"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"
    # Should delete both viewer and editor relations
    assert mock_fga.delete_relation.call_count == 2
