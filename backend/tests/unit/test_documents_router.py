"""Unit tests for the documents CRUD router with FGA enforcement."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session, SQLModel, create_engine

from app.main import app
from app.models.database import get_session
from app.models.document import Document


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture(autouse=True)
def test_db():
    """In-memory SQLite database, fresh per test."""
    engine = create_engine("sqlite://", echo=False, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    yield engine
    app.dependency_overrides.pop(get_session, None)
    SQLModel.metadata.drop_all(engine)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


AUTH_HEADER = {"Authorization": "Bearer valid.token"}

AUTHED_CLAIMS = {
    "sub": "user-1",
    "dct": "tenant-abc",
    "tenants": {"tenant-abc": {"roles": ["admin"], "permissions": []}},
}

NO_TENANT_CLAIMS = {"sub": "user-1", "tenants": {}}


def _seed_doc(engine, doc_id="doc-1", tenant_id="tenant-abc", **kwargs):
    defaults = {
        "title": "Test Doc",
        "content": "Hello",
        "created_by": "user-1",
    }
    defaults.update(kwargs)
    with Session(engine) as session:
        doc = Document(id=doc_id, tenant_id=tenant_id, **defaults)
        session.add(doc)
        session.commit()


def _make_http_error(status_code=500):
    req = httpx.Request("POST", "https://api.descope.com")
    resp = httpx.Response(status_code, request=req, text="error")
    return httpx.HTTPStatusError(f"{status_code}", request=req, response=resp)


def _make_network_error():
    req = httpx.Request("POST", "https://api.descope.com")
    return httpx.RequestError("Connection refused", request=req)


# ============================================================
# Auth enforcement
# ============================================================


@pytest.mark.anyio
async def test_create_document_unauthenticated(client):
    resp = await client.post("/api/documents", json={"title": "T"})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_list_documents_unauthenticated(client):
    resp = await client.get("/api/documents")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_get_document_unauthenticated(client):
    resp = await client.get("/api/documents/doc-1")
    assert resp.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_document_no_tenant(mock_validate, client):
    mock_validate.return_value = NO_TENANT_CLAIMS
    resp = await client.post("/api/documents", headers=AUTH_HEADER, json={"title": "T"})
    assert resp.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_document_no_sub(mock_validate, client):
    mock_validate.return_value = {
        "dct": "tenant-abc",
        "tenants": {"tenant-abc": {}},
    }
    resp = await client.post("/api/documents", headers=AUTH_HEADER, json={"title": "T"})
    assert resp.status_code == 401


# ============================================================
# POST /api/documents
# ============================================================


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_document_success(mock_validate, mock_factory, client):
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    resp = await client.post(
        "/api/documents",
        headers=AUTH_HEADER,
        json={"title": "My Doc", "content": "Body"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "My Doc"
    assert data["content"] == "Body"
    assert data["tenant_id"] == "tenant-abc"
    assert data["created_by"] == "user-1"
    assert data["id"]
    mock_client.create_relation.assert_called_once_with("document", data["id"], "owner", "user-1")


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_document_default_content(mock_validate, mock_factory, client):
    """Content defaults to empty string if not provided."""
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    resp = await client.post("/api/documents", headers=AUTH_HEADER, json={"title": "My Doc"})
    assert resp.status_code == 201
    assert resp.json()["content"] == ""


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_document_fga_http_error(mock_validate, mock_factory, client):
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_relation.side_effect = _make_http_error(500)
    mock_factory.return_value = mock_client

    resp = await client.post("/api/documents", headers=AUTH_HEADER, json={"title": "Doc"})
    assert resp.status_code == 502


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_document_fga_network_error(mock_validate, mock_factory, client):
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_relation.side_effect = _make_network_error()
    mock_factory.return_value = mock_client

    resp = await client.post("/api/documents", headers=AUTH_HEADER, json={"title": "Doc"})
    assert resp.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_document_empty_title_rejected(mock_validate, client):
    mock_validate.return_value = AUTHED_CLAIMS
    resp = await client.post("/api/documents", headers=AUTH_HEADER, json={"title": ""})
    assert resp.status_code == 422


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_document_db_failure_compensates_fga(mock_validate, mock_factory, client):
    """DB commit failure → FGA relation is compensated (deleted)."""
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    def _failing_session():
        mock_session = MagicMock()
        mock_session.commit.side_effect = Exception("DB error")
        yield mock_session

    app.dependency_overrides[get_session] = _failing_session

    resp = await client.post("/api/documents", headers=AUTH_HEADER, json={"title": "Doc"})
    assert resp.status_code == 500
    mock_client.create_relation.assert_called_once()
    mock_client.delete_relation.assert_called_once()


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_document_db_failure_compensation_also_fails(mock_validate, mock_factory, client):
    """DB commit failure + FGA compensation failure → still returns 500."""
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.delete_relation.side_effect = Exception("Compensation failed")
    mock_factory.return_value = mock_client

    def _failing_session():
        mock_session = MagicMock()
        mock_session.commit.side_effect = Exception("DB error")
        yield mock_session

    app.dependency_overrides[get_session] = _failing_session

    resp = await client.post("/api/documents", headers=AUTH_HEADER, json={"title": "Doc"})
    assert resp.status_code == 500


# ============================================================
# GET /api/documents
# ============================================================


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_documents_success(mock_validate, mock_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")
    _seed_doc(test_db, doc_id="doc-2")

    mock_client = AsyncMock()
    mock_client.list_user_resources.return_value = [
        {"resource": "doc-1"},
        {"resource": "doc-2"},
    ]
    mock_factory.return_value = mock_client

    resp = await client.get("/api/documents", headers=AUTH_HEADER)
    assert resp.status_code == 200
    docs = resp.json()["documents"]
    assert len(docs) == 2
    mock_client.list_user_resources.assert_called_once_with("document", "can_view", "user-1")


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_documents_empty(mock_validate, mock_factory, client):
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_user_resources.return_value = []
    mock_factory.return_value = mock_client

    resp = await client.get("/api/documents", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["documents"] == []


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_documents_fga_error(mock_validate, mock_factory, client):
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_user_resources.side_effect = _make_http_error(500)
    mock_factory.return_value = mock_client

    resp = await client.get("/api/documents", headers=AUTH_HEADER)
    assert resp.status_code == 502


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_documents_cross_tenant_filtered(mock_validate, mock_factory, client, test_db):
    """Docs from other tenants are filtered out even if FGA returns them."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-mine", tenant_id="tenant-abc")
    _seed_doc(test_db, doc_id="doc-other", tenant_id="tenant-xyz")

    mock_client = AsyncMock()
    mock_client.list_user_resources.return_value = [
        {"resource": "doc-mine"},
        {"resource": "doc-other"},
    ]
    mock_factory.return_value = mock_client

    resp = await client.get("/api/documents", headers=AUTH_HEADER)
    assert resp.status_code == 200
    docs = resp.json()["documents"]
    assert len(docs) == 1
    assert docs[0]["id"] == "doc-mine"


# ============================================================
# GET /api/documents/{document_id}
# ============================================================


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_document_success(mock_validate, mock_fga_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.get("/api/documents/doc-1", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["id"] == "doc-1"
    assert resp.json()["title"] == "Test Doc"


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_document_not_found(mock_validate, mock_fga_factory, client):
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.get("/api/documents/nonexistent", headers=AUTH_HEADER)
    assert resp.status_code == 404


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_document_cross_tenant(mock_validate, mock_fga_factory, client, test_db):
    """Doc exists but belongs to different tenant → 404 (not 403, prevents IDOR)."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1", tenant_id="tenant-xyz")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.get("/api/documents/doc-1", headers=AUTH_HEADER)
    assert resp.status_code == 404


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_document_fga_denied(mock_validate, mock_fga_factory, client):
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.return_value = False
    mock_fga_factory.return_value = mock_client

    resp = await client.get("/api/documents/doc-1", headers=AUTH_HEADER)
    assert resp.status_code == 403


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_document_fga_error_fail_closed(mock_validate, mock_fga_factory, client):
    """FGA API error → 502, never fail-open."""
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.side_effect = _make_http_error(500)
    mock_fga_factory.return_value = mock_client

    resp = await client.get("/api/documents/doc-1", headers=AUTH_HEADER)
    assert resp.status_code == 502


# ============================================================
# PUT /api/documents/{document_id}
# ============================================================


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_document_success(mock_validate, mock_fga_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.put(
        "/api/documents/doc-1",
        headers=AUTH_HEADER,
        json={"title": "Updated", "content": "New body"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated"
    assert resp.json()["content"] == "New body"


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_document_partial_title(mock_validate, mock_fga_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1", content="Original")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.put(
        "/api/documents/doc-1",
        headers=AUTH_HEADER,
        json={"title": "New Title"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "New Title"
    assert resp.json()["content"] == "Original"


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_document_not_found(mock_validate, mock_fga_factory, client):
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.put(
        "/api/documents/nonexistent",
        headers=AUTH_HEADER,
        json={"title": "X"},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_document_cross_tenant(mock_validate, mock_fga_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1", tenant_id="tenant-xyz")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.put(
        "/api/documents/doc-1",
        headers=AUTH_HEADER,
        json={"title": "X"},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_document_fga_denied(mock_validate, mock_fga_factory, client, test_db):
    """PUT: FGA denies can_edit → 403."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = False
    mock_fga_factory.return_value = mock_client

    resp = await client.put(
        "/api/documents/doc-1",
        headers=AUTH_HEADER,
        json={"title": "Updated"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_document_fga_error_fail_closed(mock_validate, mock_fga_factory, client, test_db):
    """PUT: FGA API error → 502, never fail-open."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.side_effect = _make_http_error(500)
    mock_fga_factory.return_value = mock_client

    resp = await client.put(
        "/api/documents/doc-1",
        headers=AUTH_HEADER,
        json={"title": "Updated"},
    )
    assert resp.status_code == 502


# ============================================================
# DELETE /api/documents/{document_id}
# ============================================================


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_document_success(mock_validate, mock_fga_factory, mock_router_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.list_relations.return_value = [
        {"relationDefinition": "owner", "target": "user-1"},
        {"relationDefinition": "viewer", "target": "user-2"},
    ]
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.delete("/api/documents/doc-1", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert mock_client.delete_relation.call_count == 2

    # Verify doc is removed from DB
    with Session(test_db) as session:
        assert session.get(Document, "doc-1") is None


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_document_not_found(mock_validate, mock_fga_factory, client):
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.delete("/api/documents/nonexistent", headers=AUTH_HEADER)
    assert resp.status_code == 404


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_document_fga_cleanup_fails(mock_validate, mock_fga_factory, mock_router_factory, client, test_db):
    """FGA cleanup failure → 502, doc NOT deleted from DB."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.list_relations.side_effect = _make_http_error(500)
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.delete("/api/documents/doc-1", headers=AUTH_HEADER)
    assert resp.status_code == 502

    # Verify doc still exists
    with Session(test_db) as session:
        assert session.get(Document, "doc-1") is not None


# ============================================================
# POST /api/documents/{document_id}/share
# ============================================================


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document_viewer_success(mock_validate, mock_fga_factory, mock_router_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.load_user.return_value = {
        "userId": "user-2",
        "userTenants": [{"tenantId": "tenant-abc"}],
    }
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.post(
        "/api/documents/doc-1/share",
        headers=AUTH_HEADER,
        json={"user_id": "user-2", "relation": "viewer"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == "user-2"
    assert data["relation"] == "viewer"
    mock_client.create_relation.assert_called_once_with("document", "doc-1", "viewer", "user-2")


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document_editor_success(mock_validate, mock_fga_factory, mock_router_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.load_user.return_value = {
        "userId": "user-2",
        "userTenants": [{"tenantId": "tenant-abc"}],
    }
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.post(
        "/api/documents/doc-1/share",
        headers=AUTH_HEADER,
        json={"user_id": "user-2", "relation": "editor"},
    )
    assert resp.status_code == 200
    assert resp.json()["relation"] == "editor"


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document_target_not_found(mock_validate, mock_fga_factory, mock_router_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.load_user.side_effect = _make_http_error(404)
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.post(
        "/api/documents/doc-1/share",
        headers=AUTH_HEADER,
        json={"user_id": "nonexistent", "relation": "viewer"},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document_target_different_tenant(
    mock_validate, mock_fga_factory, mock_router_factory, client, test_db
):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.load_user.return_value = {
        "userId": "user-2",
        "userTenants": [{"tenantId": "tenant-xyz"}],
    }
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.post(
        "/api/documents/doc-1/share",
        headers=AUTH_HEADER,
        json={"user_id": "user-2", "relation": "viewer"},
    )
    assert resp.status_code == 403
    assert "outside your tenant" in resp.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document_fga_create_fails(mock_validate, mock_fga_factory, mock_router_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.load_user.return_value = {
        "userId": "user-2",
        "userTenants": [{"tenantId": "tenant-abc"}],
    }
    mock_client.create_relation.side_effect = _make_http_error(500)
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.post(
        "/api/documents/doc-1/share",
        headers=AUTH_HEADER,
        json={"user_id": "user-2", "relation": "editor"},
    )
    assert resp.status_code == 502


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document_load_user_network_error(
    mock_validate, mock_fga_factory, mock_router_factory, client, test_db
):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.load_user.side_effect = _make_network_error()
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.post(
        "/api/documents/doc-1/share",
        headers=AUTH_HEADER,
        json={"user_id": "user-2", "relation": "viewer"},
    )
    assert resp.status_code == 502


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document_not_found(mock_validate, mock_fga_factory, client):
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.post(
        "/api/documents/nonexistent/share",
        headers=AUTH_HEADER,
        json={"user_id": "u2", "relation": "viewer"},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document_invalid_relation(mock_validate, mock_fga_factory, client):
    """Only 'viewer' and 'editor' are valid share relations."""
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.post(
        "/api/documents/doc-1/share",
        headers=AUTH_HEADER,
        json={"user_id": "u2", "relation": "admin"},
    )
    assert resp.status_code == 422


# ============================================================
# DELETE /api/documents/{document_id}/share/{target_user_id}
# ============================================================


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_revoke_share_success(mock_validate, mock_fga_factory, mock_router_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.delete("/api/documents/doc-1/share/user-2", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["status"] == "revoked"
    # Deletes both viewer and editor relations
    assert mock_client.delete_relation.call_count == 2


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_revoke_share_tolerates_400_404(mock_validate, mock_fga_factory, mock_router_factory, client, test_db):
    """400/404 from delete_relation is tolerated (relation may not exist)."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.delete_relation.side_effect = _make_http_error(400)
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.delete("/api/documents/doc-1/share/user-2", headers=AUTH_HEADER)
    assert resp.status_code == 200


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_revoke_share_http_error_502(mock_validate, mock_fga_factory, mock_router_factory, client, test_db):
    """Non-400/404 HTTP error on revoke → 502."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.delete_relation.side_effect = _make_http_error(500)
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.delete("/api/documents/doc-1/share/user-2", headers=AUTH_HEADER)
    assert resp.status_code == 502


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_revoke_share_network_error(mock_validate, mock_fga_factory, mock_router_factory, client, test_db):
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.delete_relation.side_effect = _make_network_error()
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.delete("/api/documents/doc-1/share/user-2", headers=AUTH_HEADER)
    assert resp.status_code == 502


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_revoke_share_doc_not_found(mock_validate, mock_fga_factory, client):
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.delete("/api/documents/nonexistent/share/user-2", headers=AUTH_HEADER)
    assert resp.status_code == 404


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_revoke_share_cross_tenant(mock_validate, mock_fga_factory, mock_router_factory, client, test_db):
    """Doc belongs to different tenant → 404."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1", tenant_id="tenant-xyz")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.delete("/api/documents/doc-1/share/user-2", headers=AUTH_HEADER)
    assert resp.status_code == 404


# ============================================================
# Additional edge case / review-fix tests
# ============================================================


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_documents_fga_returns_none(mock_validate, mock_factory, client):
    """list_user_resources returns None -> treated as empty list."""
    mock_validate.return_value = AUTHED_CLAIMS
    mock_client = AsyncMock()
    mock_client.list_user_resources.return_value = None
    mock_factory.return_value = mock_client

    resp = await client.get("/api/documents", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["documents"] == []


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_document_db_failure_after_fga_cleanup(
    mock_validate, mock_fga_factory, mock_router_factory, client
):
    """DB delete fails after FGA cleanup -> 500."""
    mock_validate.return_value = AUTHED_CLAIMS

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.list_relations.return_value = [
        {"relationDefinition": "owner", "target": "user-1"},
    ]
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    def _failing_session():
        mock_session = MagicMock()
        mock_session.get.return_value = Document(
            id="doc-1",
            tenant_id="tenant-abc",
            title="T",
            content="",
            created_by="user-1",
        )
        mock_session.commit.side_effect = Exception("DB commit failed")
        yield mock_session

    app.dependency_overrides[get_session] = _failing_session

    resp = await client.delete("/api/documents/doc-1", headers=AUTH_HEADER)
    assert resp.status_code == 500
    # FGA cleanup happened before DB failure
    assert mock_client.delete_relation.call_count == 1


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document_self_sharing_rejected(
    mock_validate, mock_fga_factory, mock_router_factory, client, test_db
):
    """Owner cannot share document with themselves."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.post(
        "/api/documents/doc-1/share",
        headers=AUTH_HEADER,
        json={"user_id": "user-1", "relation": "viewer"},
    )
    assert resp.status_code == 400
    assert "yourself" in resp.json()["detail"]


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document_load_user_returns_none(
    mock_validate, mock_fga_factory, mock_router_factory, client, test_db
):
    """load_user returns None -> 404."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.load_user.return_value = None
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.post(
        "/api/documents/doc-1/share",
        headers=AUTH_HEADER,
        json={"user_id": "user-2", "relation": "viewer"},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_document_no_changes(mock_validate, mock_fga_factory, client, test_db):
    """PUT with no title or content -> returns doc unchanged."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.put(
        "/api/documents/doc-1",
        headers=AUTH_HEADER,
        json={},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Test Doc"
    assert resp.json()["content"] == "Hello"


@pytest.mark.anyio
@patch("app.routers.documents.get_descope_client")
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_document_fga_relations_none(
    mock_validate, mock_fga_factory, mock_router_factory, client, test_db
):
    """list_relations returns None -> treated as empty, delete succeeds."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.list_relations.return_value = None
    mock_fga_factory.return_value = mock_client
    mock_router_factory.return_value = mock_client

    resp = await client.delete("/api/documents/doc-1", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    mock_client.delete_relation.assert_not_called()


# ============================================================
# Permission derivation — verify correct FGA relations checked
# ============================================================


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_get_document_checks_can_view(mock_validate, mock_fga_factory, client, test_db):
    """AC5: GET /documents/{id} checks can_view relation (derived from owner/editor)."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.get("/api/documents/doc-1", headers=AUTH_HEADER)
    assert resp.status_code == 200
    mock_client.check_permission.assert_called_once_with("document", "doc-1", "can_view", "user-1")


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_document_checks_can_edit(mock_validate, mock_fga_factory, client, test_db):
    """AC5: PUT /documents/{id} checks can_edit relation (derived from owner)."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_fga_factory.return_value = mock_client

    resp = await client.put(
        "/api/documents/doc-1",
        headers=AUTH_HEADER,
        json={"title": "Updated"},
    )
    assert resp.status_code == 200
    mock_client.check_permission.assert_called_once_with("document", "doc-1", "can_edit", "user-1")


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_delete_document_checks_can_delete(mock_validate, mock_fga_factory, client, test_db):
    """AC5: DELETE /documents/{id} checks can_delete relation."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.list_relations.return_value = []
    mock_fga_factory.return_value = mock_client

    # Need to also mock router-level get_descope_client for delete endpoint
    with patch("app.routers.documents.get_descope_client", return_value=mock_client):
        resp = await client.delete("/api/documents/doc-1", headers=AUTH_HEADER)
    assert resp.status_code == 200
    mock_client.check_permission.assert_called_once_with("document", "doc-1", "can_delete", "user-1")


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_share_document_checks_owner(mock_validate, mock_fga_factory, client, test_db):
    """AC5: POST /documents/{id}/share checks owner relation."""
    mock_validate.return_value = AUTHED_CLAIMS
    _seed_doc(test_db, doc_id="doc-1")

    mock_client = AsyncMock()
    mock_client.check_permission.return_value = True
    mock_client.load_user.return_value = {
        "userId": "user-2",
        "userTenants": [{"tenantId": "tenant-abc"}],
    }
    mock_fga_factory.return_value = mock_client

    with patch("app.routers.documents.get_descope_client", return_value=mock_client):
        resp = await client.post(
            "/api/documents/doc-1/share",
            headers=AUTH_HEADER,
            json={"user_id": "user-2", "relation": "viewer"},
        )
    assert resp.status_code == 200
    mock_client.check_permission.assert_called_once_with("document", "doc-1", "owner", "user-1")


@pytest.mark.anyio
@patch("app.dependencies.fga.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_documents_checks_can_view_relation(mock_validate, mock_fga_factory, client):
    """AC5: GET /documents uses can_view derived relation for list_user_resources."""
    mock_validate.return_value = AUTHED_CLAIMS

    mock_client = AsyncMock()
    mock_client.list_user_resources.return_value = []
    mock_fga_factory.return_value = mock_client

    with patch("app.routers.documents.get_descope_client", return_value=mock_client):
        resp = await client.get("/api/documents", headers=AUTH_HEADER)
    assert resp.status_code == 200
    mock_client.list_user_resources.assert_called_once_with("document", "can_view", "user-1")
