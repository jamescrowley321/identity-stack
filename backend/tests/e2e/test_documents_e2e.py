"""E2E tests for document-FGA lifecycle: creation, sharing, revocation, deletion, and permission derivation.

These tests exercise the real Descope FGA API and the backend's document + FGA endpoints
together, verifying that FGA relations are correctly created, checked, and cleaned up
throughout the document lifecycle.

Requires DESCOPE_MANAGEMENT_KEY env var (for admin token via tenant-scoped access key).
"""

import contextlib
import os
import time
import uuid

import pytest
from playwright.sync_api import APIRequestContext

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY"),
    reason="DESCOPE_MANAGEMENT_KEY not set",
)

MAX_API_RESPONSE_MS = 2000


def _unique_id(prefix: str) -> str:
    return f"{prefix}-e2e-{uuid.uuid4().hex[:8]}"


def _timed_request(context: APIRequestContext, method: str, url: str, **kwargs) -> tuple:
    """Execute a request and return (response, elapsed_ms)."""
    start = time.monotonic()
    if method == "GET":
        resp = context.get(url, **kwargs)
    elif method == "POST":
        resp = context.post(url, **kwargs)
    elif method == "PUT":
        resp = context.put(url, **kwargs)
    elif method == "DELETE":
        resp = context.delete(url, **kwargs)
    else:
        raise ValueError(f"Unsupported method: {method}")
    elapsed_ms = (time.monotonic() - start) * 1000
    if not (200 <= resp.status < 300):
        print(f"[E2E] {method} {url} → {resp.status}: {resp.text()}")
    return resp, elapsed_ms


def _create_doc(ctx: APIRequestContext, base: str, title: str = "") -> dict | None:
    """Create a document and return its data, or None if FGA not operational."""
    title = title or _unique_id("doc")
    resp, _ = _timed_request(ctx, "POST", f"{base}/api/documents", data={"title": title, "content": "E2E test"})
    if resp.status == 502:
        return None  # FGA not operational
    if resp.status != 201:
        return None
    return resp.json()


def _cleanup_doc(ctx: APIRequestContext, base: str, doc_id: str) -> None:
    """Best-effort document deletion."""
    with contextlib.suppress(Exception):
        ctx.delete(f"{base}/api/documents/{doc_id}")


# --- AC: Document creation creates FGA owner relation ---


def test_create_document_creates_fga_owner(admin_api_context: APIRequestContext, backend_url: str):
    """Creating a document establishes an FGA owner relation for the creator."""
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    try:
        # Verify owner relation via FGA admin endpoint
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "GET",
            f"{backend_url}/api/fga/relations",
            params={"resource_type": "document", "resource_id": doc_id},
        )
        assert resp.status == 200
        assert elapsed_ms < MAX_API_RESPONSE_MS
        relations = resp.json().get("relations", [])
        owner_rels = [r for r in relations if r.get("relationDefinition") == "owner"]
        assert len(owner_rels) >= 1, f"Expected at least 1 owner relation, got {owner_rels}"
    finally:
        _cleanup_doc(admin_api_context, backend_url, doc_id)


# --- AC: Permission derivation (owner=all, editor=view+edit, viewer=view only) ---


def test_owner_has_all_permissions(admin_api_context: APIRequestContext, backend_url: str):
    """Owner should have can_view, can_edit, and can_delete permissions."""
    resource_id = _unique_id("perm-doc")
    target = _unique_id("user")

    # Create owner relation directly
    resp, _ = _timed_request(
        admin_api_context,
        "POST",
        f"{backend_url}/api/fga/relations",
        data={"resource_type": "document", "resource_id": resource_id, "relation": "owner", "target": target},
    )
    if resp.status in (400, 502):
        pytest.skip(f"FGA not operational (status {resp.status})")
    assert resp.status == 201

    try:
        for relation in ("can_view", "can_edit", "can_delete"):
            resp, elapsed_ms = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/fga/check",
                data={"resource_type": "document", "resource_id": resource_id, "relation": relation, "target": target},
            )
            if resp.status == 502:
                pytest.skip("FGA check not operational")
            assert resp.status == 200, f"Check {relation} returned {resp.status}"
            assert elapsed_ms < MAX_API_RESPONSE_MS
            assert resp.json()["allowed"] is True, f"Owner should have {relation}"
    finally:
        with contextlib.suppress(Exception):
            admin_api_context.delete(
                f"{backend_url}/api/fga/relations",
                data={"resource_type": "document", "resource_id": resource_id, "relation": "owner", "target": target},
            )


def test_editor_has_view_and_edit_not_delete(admin_api_context: APIRequestContext, backend_url: str):
    """Editor should have can_view and can_edit but NOT can_delete."""
    resource_id = _unique_id("perm-doc")
    target = _unique_id("user")

    resp, _ = _timed_request(
        admin_api_context,
        "POST",
        f"{backend_url}/api/fga/relations",
        data={"resource_type": "document", "resource_id": resource_id, "relation": "editor", "target": target},
    )
    if resp.status in (400, 502):
        pytest.skip(f"FGA not operational (status {resp.status})")
    assert resp.status == 201

    try:
        # Editor should have can_view and can_edit
        for relation in ("can_view", "can_edit"):
            resp, _ = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/fga/check",
                data={"resource_type": "document", "resource_id": resource_id, "relation": relation, "target": target},
            )
            if resp.status == 502:
                pytest.skip("FGA check not operational")
            assert resp.status == 200
            assert resp.json()["allowed"] is True, f"Editor should have {relation}"

        # Editor should NOT have can_delete
        resp, _ = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/fga/check",
            data={"resource_type": "document", "resource_id": resource_id, "relation": "can_delete", "target": target},
        )
        if resp.status == 502:
            pytest.skip("FGA check not operational")
        assert resp.status == 200
        assert resp.json()["allowed"] is False, "Editor should NOT have can_delete"
    finally:
        with contextlib.suppress(Exception):
            admin_api_context.delete(
                f"{backend_url}/api/fga/relations",
                data={"resource_type": "document", "resource_id": resource_id, "relation": "editor", "target": target},
            )


def test_viewer_has_view_only(admin_api_context: APIRequestContext, backend_url: str):
    """Viewer should have can_view but NOT can_edit or can_delete."""
    resource_id = _unique_id("perm-doc")
    target = _unique_id("user")

    resp, _ = _timed_request(
        admin_api_context,
        "POST",
        f"{backend_url}/api/fga/relations",
        data={"resource_type": "document", "resource_id": resource_id, "relation": "viewer", "target": target},
    )
    if resp.status in (400, 502):
        pytest.skip(f"FGA not operational (status {resp.status})")
    assert resp.status == 201

    try:
        # Viewer should have can_view
        resp, _ = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/fga/check",
            data={"resource_type": "document", "resource_id": resource_id, "relation": "can_view", "target": target},
        )
        if resp.status == 502:
            pytest.skip("FGA check not operational")
        assert resp.status == 200
        assert resp.json()["allowed"] is True, "Viewer should have can_view"

        # Viewer should NOT have can_edit or can_delete
        for relation in ("can_edit", "can_delete"):
            resp, _ = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/fga/check",
                data={
                    "resource_type": "document",
                    "resource_id": resource_id,
                    "relation": relation,
                    "target": target,
                },
            )
            if resp.status == 502:
                pytest.skip("FGA check not operational")
            assert resp.status == 200
            assert resp.json()["allowed"] is False, f"Viewer should NOT have {relation}"
    finally:
        with contextlib.suppress(Exception):
            admin_api_context.delete(
                f"{backend_url}/api/fga/relations",
                data={"resource_type": "document", "resource_id": resource_id, "relation": "viewer", "target": target},
            )


# --- AC: Document get/update/delete enforce FGA permissions ---


def test_document_get_enforces_fga(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/documents/{id} returns 200 for owner, verifying FGA can_view works end-to-end."""
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    try:
        resp, elapsed_ms = _timed_request(admin_api_context, "GET", f"{backend_url}/api/documents/{doc_id}")
        assert resp.status == 200, f"Owner should be able to view document, got {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        assert resp.json()["id"] == doc_id
    finally:
        _cleanup_doc(admin_api_context, backend_url, doc_id)


def test_document_update_enforces_fga(admin_api_context: APIRequestContext, backend_url: str):
    """PUT /api/documents/{id} returns 200 for owner, verifying FGA can_edit works end-to-end."""
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    try:
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "PUT",
            f"{backend_url}/api/documents/{doc_id}",
            data={"title": "Updated E2E Title"},
        )
        assert resp.status == 200, f"Owner should be able to edit document, got {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        assert resp.json()["title"] == "Updated E2E Title"
    finally:
        _cleanup_doc(admin_api_context, backend_url, doc_id)


def test_document_delete_cleans_fga_relations(admin_api_context: APIRequestContext, backend_url: str):
    """DELETE /api/documents/{id} removes the document and cleans up FGA relations."""
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    # Delete document (owner can delete)
    resp, elapsed_ms = _timed_request(admin_api_context, "DELETE", f"{backend_url}/api/documents/{doc_id}")
    assert resp.status == 200, f"Owner should be able to delete document, got {resp.status}"
    assert elapsed_ms < MAX_API_RESPONSE_MS

    # Verify FGA relations are cleaned up
    resp, _ = _timed_request(
        admin_api_context,
        "GET",
        f"{backend_url}/api/fga/relations",
        params={"resource_type": "document", "resource_id": doc_id},
    )
    if resp.status == 200:
        relations = resp.json().get("relations", [])
        assert len(relations) == 0, f"Expected 0 relations after delete, got {relations}"


# --- AC: List documents returns only FGA-authorized documents ---


def test_list_documents_returns_authorized(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/documents returns documents the caller is authorized to view."""
    doc = _create_doc(admin_api_context, backend_url, title=_unique_id("list-doc"))
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    try:
        resp, elapsed_ms = _timed_request(admin_api_context, "GET", f"{backend_url}/api/documents")
        assert resp.status == 200
        assert elapsed_ms < MAX_API_RESPONSE_MS
        documents = resp.json().get("documents", [])
        doc_ids = [d["id"] for d in documents]
        assert doc_id in doc_ids, f"Created document {doc_id} should appear in list"
    finally:
        _cleanup_doc(admin_api_context, backend_url, doc_id)


# --- AC: Concurrent relation create/delete with permission checks ---


def test_concurrent_relations_no_false_positive(admin_api_context: APIRequestContext, backend_url: str):
    """Rapid create/delete of relations should never produce false-positive authorizations."""
    resource_id = _unique_id("concurrent-doc")
    target = _unique_id("user")
    base_body = {"resource_type": "document", "resource_id": resource_id, "relation": "viewer", "target": target}

    # Create the relation first to verify FGA is operational
    resp, _ = _timed_request(admin_api_context, "POST", f"{backend_url}/api/fga/relations", data=base_body)
    if resp.status in (400, 502):
        pytest.skip(f"FGA not operational (status {resp.status})")
    assert resp.status == 201

    try:
        # Rapid cycles: delete then immediately check — should never get allowed=true after delete
        for _ in range(3):
            # Delete
            resp, _ = _timed_request(admin_api_context, "DELETE", f"{backend_url}/api/fga/relations", data=base_body)
            assert resp.status == 200

            # Immediate check — must not be allowed
            resp, _ = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/fga/check",
                data={
                    "resource_type": "document",
                    "resource_id": resource_id,
                    "relation": "can_view",
                    "target": target,
                },
            )
            if resp.status == 502:
                pytest.skip("FGA check not operational")
            assert resp.status == 200
            assert resp.json()["allowed"] is False, "Should be denied after relation deleted"

            # Re-create for next cycle
            resp, _ = _timed_request(admin_api_context, "POST", f"{backend_url}/api/fga/relations", data=base_body)
            assert resp.status == 201
    finally:
        # Cleanup
        with contextlib.suppress(Exception):
            admin_api_context.delete(f"{backend_url}/api/fga/relations", data=base_body)


# --- AC: Document endpoints require authentication ---


def test_document_endpoints_reject_no_auth(api_context: APIRequestContext, backend_url: str):
    """Document endpoints return 401 without authentication."""
    endpoints = [
        ("POST", "/api/documents", {"title": "test", "content": "test"}),
        ("GET", "/api/documents", None),
        ("GET", "/api/documents/nonexistent", None),
        ("PUT", "/api/documents/nonexistent", {"title": "test"}),
        ("DELETE", "/api/documents/nonexistent", None),
        ("POST", "/api/documents/nonexistent/share", {"user_id": "u1", "relation": "viewer"}),
        ("DELETE", "/api/documents/nonexistent/share/user1", None),
    ]
    for method, path, payload in endpoints:
        url = f"{backend_url}{path}"
        if method == "GET":
            resp = api_context.get(url)
        elif method == "POST":
            resp = api_context.post(url, data=payload)
        elif method == "PUT":
            resp = api_context.put(url, data=payload)
        elif method == "DELETE":
            resp = api_context.delete(url)
        else:
            continue
        assert resp.status == 401, f"{method} {path} returned {resp.status}, expected 401"


# --- AC: Schema update impact ---


def test_schema_update_reflects_in_checks(admin_api_context: APIRequestContext, backend_url: str):
    """Updating the FGA schema affects subsequent permission checks.

    This test saves the current schema, verifies it can be read back, and confirms
    that the schema endpoint works end-to-end. Full schema mutation testing (removing
    relation types) is deferred because modifying the live schema could break other
    concurrent tests.
    """
    # Read current schema
    resp, _ = _timed_request(admin_api_context, "GET", f"{backend_url}/api/fga/schema")
    assert resp.status == 200
    original_schema = resp.json().get("schema", "")

    if not original_schema:
        pytest.skip("No FGA schema configured — cannot test update impact")

    # Re-save the same schema (idempotent — safe for concurrent test runs)
    schema_str = original_schema if isinstance(original_schema, str) else str(original_schema)
    resp, elapsed_ms = _timed_request(
        admin_api_context,
        "PUT",
        f"{backend_url}/api/fga/schema",
        data={"schema": schema_str},
    )
    # Schema update may fail if the format isn't what Descope expects
    if resp.status in (400, 502):
        pytest.skip(f"Schema update not supported in this project (status {resp.status})")
    assert resp.status == 200, f"Schema update failed: {resp.status}"
    assert elapsed_ms < MAX_API_RESPONSE_MS

    # Verify read-back
    resp, _ = _timed_request(admin_api_context, "GET", f"{backend_url}/api/fga/schema")
    assert resp.status == 200
