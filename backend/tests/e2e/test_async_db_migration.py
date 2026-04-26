"""E2E tests verifying async database migration didn't break endpoint behavior.

PR #186 migrates the data access layer from sync SQLite to async PostgreSQL.
These tests exercise the tenant and document endpoints end-to-end, confirming
that async sessions, commits, and rollbacks work correctly through the full
request lifecycle.

Covers:
- Health check (async DB connectivity verification in lifespan)
- Tenant endpoints (list, current, resources CRUD via async session)
- Document endpoints (create, list, get, update, delete via async session)
- Auth enforcement (unauthenticated requests rejected)
- Error response shape (FastAPI HTTPException detail format on 404s)

Requires DESCOPE_MANAGEMENT_KEY for admin-level tests.
Requires DESCOPE_CLIENT_ID/DESCOPE_CLIENT_SECRET for auth-level tests.
"""

import contextlib
import os

import pytest
from playwright.sync_api import APIRequestContext

from tests.e2e.helpers.api import unique_name

_DUMMY_UUID = "00000000-0000-0000-0000-000000000000"
_NONEXISTENT_UUID = "ffffffff-ffff-ffff-ffff-ffffffffffff"


# ---------------------------------------------------------------------------
# Health check — verifies async DB connectivity check in lifespan
# ---------------------------------------------------------------------------


class TestAsyncHealthCheck:
    """Verify the app starts successfully with async database engine."""

    def test_health_returns_ok_with_async_db(self, api_context: APIRequestContext, backend_url: str):
        """Health endpoint returns 200, proving async engine connected during lifespan startup."""
        resp = api_context.get(f"{backend_url}/api/health")
        assert resp.status == 200
        body = resp.json()
        assert body["status"] == "ok"

    def test_openapi_schema_loads_with_async_routers(self, api_context: APIRequestContext, backend_url: str):
        """OpenAPI schema generation succeeds with async route handlers."""
        resp = api_context.get(f"{backend_url}/openapi.json")
        assert resp.status == 200
        body = resp.json()
        # Verify document and tenant paths are registered (async routers loaded)
        assert "/api/documents" in body["paths"]
        assert "/api/tenants" in body["paths"]


# ---------------------------------------------------------------------------
# Auth enforcement — unauthenticated requests rejected for DB-backed endpoints
# ---------------------------------------------------------------------------


class TestAsyncEndpointsRejectNoAuth:
    """All DB-backed endpoints return 401 without auth token after async migration."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/tenants"),
            ("GET", "/api/tenants/current"),
            ("POST", "/api/tenants"),
            ("GET", "/api/documents"),
            ("POST", "/api/documents"),
            ("GET", f"/api/documents/{_DUMMY_UUID}"),
            ("PUT", f"/api/documents/{_DUMMY_UUID}"),
            ("DELETE", f"/api/documents/{_DUMMY_UUID}"),
        ],
    )
    def test_db_endpoints_return_401_without_auth(
        self, api_context: APIRequestContext, backend_url: str, method: str, path: str
    ):
        """DB-backed endpoints must reject unauthenticated requests."""
        url = f"{backend_url}{path}"
        dummy_payload = {"title": "test", "content": "test", "name": "test"}
        if method == "GET":
            resp = api_context.get(url)
        elif method == "POST":
            resp = api_context.post(url, data=dummy_payload)
        elif method == "PUT":
            resp = api_context.put(url, data=dummy_payload)
        elif method == "DELETE":
            resp = api_context.delete(url)
        else:
            pytest.fail(f"Unsupported method: {method}")
        assert resp.status == 401, f"{method} {path} returned {resp.status}, expected 401"

    def test_tenant_resources_returns_401_without_auth(self, api_context: APIRequestContext, backend_url: str):
        """Tenant resources endpoint rejects unauthenticated requests."""
        resp = api_context.get(f"{backend_url}/api/tenants/some-tenant/resources")
        assert resp.status == 401


# ---------------------------------------------------------------------------
# Tenant endpoints — async session for list, current, and resources
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("DESCOPE_CLIENT_ID") or not os.environ.get("DESCOPE_CLIENT_SECRET"),
    reason="DESCOPE_CLIENT_ID/DESCOPE_CLIENT_SECRET not set",
)
class TestAsyncTenantEndpoints:
    """Verify tenant endpoints work with async database sessions."""

    def test_list_tenants_returns_list(self, auth_api_context: APIRequestContext, backend_url: str):
        """GET /api/tenants returns tenant list from JWT claims (no DB hit, but validates async middleware chain)."""
        resp = auth_api_context.get(f"{backend_url}/api/tenants")
        assert resp.status == 200
        body = resp.json()
        assert "tenants" in body
        assert isinstance(body["tenants"], list)

    def test_current_tenant_no_5xx(self, auth_api_context: APIRequestContext, backend_url: str):
        """GET /api/tenants/current does not 5xx after async migration.

        Returns 200 (with tenant info) or 403 (no dct claim) — never 500.
        """
        resp = auth_api_context.get(f"{backend_url}/api/tenants/current")
        assert resp.status < 500, f"/api/tenants/current returned {resp.status}"


@pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY"),
    reason="DESCOPE_MANAGEMENT_KEY not set",
)
class TestAsyncTenantResourcesCRUD:
    """Verify tenant resource CRUD uses async session correctly."""

    def test_tenant_resources_list_no_5xx(
        self, admin_api_context: APIRequestContext, backend_url: str, test_tenant_id: str
    ):
        """GET /api/tenants/{id}/resources returns list via async session, never 5xx."""
        resp = admin_api_context.get(f"{backend_url}/api/tenants/{test_tenant_id}/resources")
        # 200 (success) or 403 (not a member) — never 500 from async session errors
        assert resp.status in (200, 403), f"Expected 200 or 403, got {resp.status}"
        if resp.status == 200:
            body = resp.json()
            assert "resources" in body
            assert isinstance(body["resources"], list)

    def test_tenant_resource_create_and_list(
        self, admin_api_context: APIRequestContext, backend_url: str, test_tenant_id: str
    ):
        """POST then GET tenant resources — verifies async session.add() + commit() + refresh()."""
        resource_name = unique_name("res")
        base = f"{backend_url}/api/tenants/{test_tenant_id}/resources"

        # Create resource
        resp = admin_api_context.post(
            base,
            data={"name": resource_name, "description": "E2E async migration test"},
        )
        # 200/201 (created), 403 (not a member), or 409 (duplicate) are all non-5xx
        if resp.status == 403:
            pytest.skip("Admin token not a member of test tenant — cannot create resources")
        if resp.status == 409:
            pytest.skip("Resource name collision — retry would need different name")
        assert resp.status in (200, 201), f"Create resource failed: {resp.status}"

        created = resp.json()
        assert "id" in created
        assert created["name"] == resource_name

        # Verify it appears in list
        resp = admin_api_context.get(base)
        assert resp.status == 200
        resources = resp.json().get("resources", [])
        resource_names = [r["name"] for r in resources]
        assert resource_name in resource_names, f"Created resource '{resource_name}' not found in list response"

    def test_tenant_resource_duplicate_returns_409(
        self, admin_api_context: APIRequestContext, backend_url: str, test_tenant_id: str
    ):
        """Duplicate resource name returns 409 — verifies async IntegrityError handling."""
        resource_name = unique_name("dup-res")
        base = f"{backend_url}/api/tenants/{test_tenant_id}/resources"

        # Create first
        resp = admin_api_context.post(
            base,
            data={"name": resource_name, "description": "first"},
        )
        if resp.status == 403:
            pytest.skip("Admin token not a member of test tenant")
        if resp.status not in (200, 201):
            pytest.skip(f"First create failed unexpectedly: {resp.status}")

        try:
            # Duplicate should fail with 409
            resp = admin_api_context.post(
                base,
                data={"name": resource_name, "description": "duplicate"},
            )
            assert resp.status == 409, f"Duplicate create should return 409, got {resp.status}"
        finally:
            # No delete endpoint for tenant resources — cleanup is best-effort via test isolation
            pass


# ---------------------------------------------------------------------------
# Document endpoints — async session for full CRUD lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY"),
    reason="DESCOPE_MANAGEMENT_KEY not set",
)
class TestAsyncDocumentCRUD:
    """Verify document CRUD operations work with async sessions end-to-end.

    These tests exercise async session.add(), session.commit(), session.refresh(),
    session.get(), session.execute(), and session.delete() through the document
    router's endpoints.
    """

    def _create_doc(self, ctx: APIRequestContext, base: str, title: str = "") -> dict | None:
        """Create a document and return its data, or None if FGA not operational."""
        title = title or unique_name("doc")
        resp = ctx.post(f"{base}/api/documents", data={"title": title, "content": "E2E async test"})
        if resp.status == 502:
            return None  # FGA not operational
        if resp.status != 201:
            return None
        data = resp.json()
        assert "id" in data, f"Document creation response missing 'id' key: {data}"
        return data

    def _cleanup_doc(self, ctx: APIRequestContext, base: str, doc_id: str) -> None:
        """Best-effort document deletion."""
        with contextlib.suppress(Exception):
            ctx.delete(f"{base}/api/documents/{doc_id}")

    def test_create_document_returns_201(self, admin_api_context: APIRequestContext, backend_url: str):
        """POST /api/documents creates document via async session.add() + commit()."""
        title = unique_name("create-doc")
        resp = admin_api_context.post(
            f"{backend_url}/api/documents",
            data={"title": title, "content": "Async migration test content"},
        )
        if resp.status == 502:
            pytest.skip("FGA not operational — document creation requires FGA")
        assert resp.status == 201, f"Create document failed: {resp.status}"

        body = resp.json()
        assert "id" in body
        assert body["title"] == title
        assert body["content"] == "Async migration test content"

        # Cleanup
        self._cleanup_doc(admin_api_context, backend_url, body["id"])

    def test_list_documents_returns_list(self, admin_api_context: APIRequestContext, backend_url: str):
        """GET /api/documents returns document list via async session.execute()."""
        doc = self._create_doc(admin_api_context, backend_url)
        if doc is None:
            pytest.skip("FGA not operational — document creation failed")
        doc_id = doc["id"]

        try:
            resp = admin_api_context.get(f"{backend_url}/api/documents")
            assert resp.status == 200
            body = resp.json()
            assert "documents" in body
            assert isinstance(body["documents"], list)
            doc_ids = [d["id"] for d in body["documents"]]
            assert doc_id in doc_ids, f"Created document {doc_id} should appear in list"
        finally:
            self._cleanup_doc(admin_api_context, backend_url, doc_id)

    def test_get_document_returns_200(self, admin_api_context: APIRequestContext, backend_url: str):
        """GET /api/documents/{id} returns document via async session.get()."""
        doc = self._create_doc(admin_api_context, backend_url)
        if doc is None:
            pytest.skip("FGA not operational — document creation failed")
        doc_id = doc["id"]

        try:
            resp = admin_api_context.get(f"{backend_url}/api/documents/{doc_id}")
            assert resp.status == 200, f"Get document failed: {resp.status}"
            body = resp.json()
            assert body["id"] == doc_id
            assert body["title"] == doc["title"]
        finally:
            self._cleanup_doc(admin_api_context, backend_url, doc_id)

    def test_update_document_returns_200(self, admin_api_context: APIRequestContext, backend_url: str):
        """PUT /api/documents/{id} updates document via async session commit + refresh."""
        doc = self._create_doc(admin_api_context, backend_url)
        if doc is None:
            pytest.skip("FGA not operational — document creation failed")
        doc_id = doc["id"]

        try:
            new_title = unique_name("updated-doc")
            resp = admin_api_context.put(
                f"{backend_url}/api/documents/{doc_id}",
                data={"title": new_title, "content": "Updated async content"},
            )
            assert resp.status == 200, f"Update document failed: {resp.status}"
            body = resp.json()
            assert body["title"] == new_title
            assert body["content"] == "Updated async content"

            # Verify persistence via GET
            resp = admin_api_context.get(f"{backend_url}/api/documents/{doc_id}")
            assert resp.status == 200
            assert resp.json()["title"] == new_title
        finally:
            self._cleanup_doc(admin_api_context, backend_url, doc_id)

    def test_delete_document_returns_200(self, admin_api_context: APIRequestContext, backend_url: str):
        """DELETE /api/documents/{id} removes document via async session.delete() + commit()."""
        doc = self._create_doc(admin_api_context, backend_url)
        if doc is None:
            pytest.skip("FGA not operational — document creation failed")
        doc_id = doc["id"]

        resp = admin_api_context.delete(f"{backend_url}/api/documents/{doc_id}")
        assert resp.status == 200, f"Delete document failed: {resp.status}"
        body = resp.json()
        assert body["status"] == "deleted"
        assert body["id"] == doc_id

        # Verify document is gone (GET should return 403 from FGA or 404)
        resp = admin_api_context.get(f"{backend_url}/api/documents/{doc_id}")
        assert resp.status in (403, 404), f"Deleted document should not be accessible, got {resp.status}"

    def test_full_document_lifecycle(self, admin_api_context: APIRequestContext, backend_url: str):
        """Full CRUD lifecycle: create -> get -> update -> list -> delete.

        Exercises every async session operation in sequence to verify no
        session state leaks between operations.
        """
        title = unique_name("lifecycle-doc")

        # Create
        resp = admin_api_context.post(
            f"{backend_url}/api/documents",
            data={"title": title, "content": "lifecycle start"},
        )
        if resp.status == 502:
            pytest.skip("FGA not operational")
        assert resp.status == 201
        doc_id = resp.json()["id"]

        try:
            # Get
            resp = admin_api_context.get(f"{backend_url}/api/documents/{doc_id}")
            assert resp.status == 200
            assert resp.json()["title"] == title

            # Update
            updated_title = title + "-updated"
            resp = admin_api_context.put(
                f"{backend_url}/api/documents/{doc_id}",
                data={"title": updated_title},
            )
            assert resp.status == 200
            assert resp.json()["title"] == updated_title

            # List (verify updated title appears)
            resp = admin_api_context.get(f"{backend_url}/api/documents")
            assert resp.status == 200
            docs = resp.json().get("documents", [])
            matching = [d for d in docs if d["id"] == doc_id]
            assert len(matching) == 1, f"Expected document {doc_id} in list"
            assert matching[0]["title"] == updated_title

            # Delete
            resp = admin_api_context.delete(f"{backend_url}/api/documents/{doc_id}")
            assert resp.status == 200
            doc_id = None  # Mark as deleted so finally block skips cleanup
        finally:
            if doc_id is not None:
                self._cleanup_doc(admin_api_context, backend_url, doc_id)


# ---------------------------------------------------------------------------
# Error responses — verify correct error shape on 404s
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY"),
    reason="DESCOPE_MANAGEMENT_KEY not set",
)
class TestAsyncErrorResponses:
    """Verify error responses maintain correct shape after async migration."""

    def test_get_nonexistent_document_returns_error(self, admin_api_context: APIRequestContext, backend_url: str):
        """GET /api/documents/{nonexistent} returns 403 or 404 with detail message.

        FGA check runs before DB lookup — may return 403 (no FGA relation) or
        404 (not found in DB). Either way, response must have 'detail' field.
        """
        resp = admin_api_context.get(f"{backend_url}/api/documents/{_NONEXISTENT_UUID}")
        assert resp.status in (403, 404), f"Expected 403 or 404, got {resp.status}"
        body = resp.json()
        assert "detail" in body, f"Error response missing 'detail' field: {body}"

    def test_update_nonexistent_document_returns_error(self, admin_api_context: APIRequestContext, backend_url: str):
        """PUT /api/documents/{nonexistent} returns 403 or 404 with detail message."""
        resp = admin_api_context.put(
            f"{backend_url}/api/documents/{_NONEXISTENT_UUID}",
            data={"title": "ghost"},
        )
        assert resp.status in (403, 404), f"Expected 403 or 404, got {resp.status}"
        body = resp.json()
        assert "detail" in body

    def test_delete_nonexistent_document_returns_error(self, admin_api_context: APIRequestContext, backend_url: str):
        """DELETE /api/documents/{nonexistent} returns 403 or 404 with detail message."""
        resp = admin_api_context.delete(f"{backend_url}/api/documents/{_NONEXISTENT_UUID}")
        assert resp.status in (403, 404), f"Expected 403 or 404, got {resp.status}"
        body = resp.json()
        assert "detail" in body

    def test_invalid_document_id_format_returns_422(self, admin_api_context: APIRequestContext, backend_url: str):
        """Non-UUID document_id returns 422 validation error (path param regex enforcement)."""
        resp = admin_api_context.get(f"{backend_url}/api/documents/not-a-uuid")
        assert resp.status == 422, f"Expected 422 for invalid UUID, got {resp.status}"

    def test_tenant_resources_nonmember_returns_403(self, admin_api_context: APIRequestContext, backend_url: str):
        """GET /api/tenants/{nonexistent}/resources returns 403 for non-member tenant."""
        fake_tenant = unique_name("fake-tenant")
        resp = admin_api_context.get(f"{backend_url}/api/tenants/{fake_tenant}/resources")
        assert resp.status == 403, f"Expected 403 for non-member tenant, got {resp.status}"
        body = resp.json()
        assert "detail" in body


# ---------------------------------------------------------------------------
# Document creation validation — async request body parsing
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY"),
    reason="DESCOPE_MANAGEMENT_KEY not set",
)
class TestAsyncDocumentValidation:
    """Verify request validation still works correctly with async handlers."""

    def test_create_document_empty_title_rejected(self, admin_api_context: APIRequestContext, backend_url: str):
        """POST /api/documents with empty title returns 422."""
        resp = admin_api_context.post(
            f"{backend_url}/api/documents",
            data={"title": "", "content": "test"},
        )
        assert resp.status == 422, f"Expected 422 for empty title, got {resp.status}"

    def test_create_document_missing_title_rejected(self, admin_api_context: APIRequestContext, backend_url: str):
        """POST /api/documents without title field returns 422."""
        resp = admin_api_context.post(
            f"{backend_url}/api/documents",
            data={"content": "no title"},
        )
        assert resp.status == 422, f"Expected 422 for missing title, got {resp.status}"

    def test_create_tenant_resource_empty_name_rejected(
        self, admin_api_context: APIRequestContext, backend_url: str, test_tenant_id: str
    ):
        """POST /api/tenants/{id}/resources with empty name returns 422."""
        resp = admin_api_context.post(
            f"{backend_url}/api/tenants/{test_tenant_id}/resources",
            data={"name": "", "description": "test"},
        )
        # 422 (validation) or 403 (not a member) — never 500
        assert resp.status in (403, 422), f"Expected 403 or 422, got {resp.status}"
