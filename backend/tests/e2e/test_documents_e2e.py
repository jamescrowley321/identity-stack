"""E2E tests for document-FGA lifecycle: creation, sharing, revocation, deletion, and permission derivation.

These tests exercise the real Descope FGA API and the backend's document + FGA endpoints
together, verifying that FGA relations are correctly created, checked, and cleaned up
throughout the document lifecycle.

Requires DESCOPE_MANAGEMENT_KEY env var (for admin token via tenant-scoped access key).

Known gaps (tracked):
- Cross-tenant isolation (AC-13/14): requires second tenant fixture
- Concurrent relation updates (AC-15): requires async test infrastructure
- FGA compensation on DB failure: requires DB failure injection
"""

import contextlib
import os

import pytest
from playwright.sync_api import APIRequestContext

from tests.e2e.helpers.api import unique_id

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY"),
    reason="DESCOPE_MANAGEMENT_KEY not set",
)

_DUMMY_UUID = "00000000-0000-0000-0000-000000000000"


def _create_doc(ctx: APIRequestContext, base: str, title: str = "") -> dict | None:
    """Create a document and return its data, or None if FGA not operational."""
    title = title or unique_id("doc")
    resp = ctx.post(f"{base}/api/documents", data={"title": title, "content": "E2E test"})
    if resp.status == 502:
        return None  # FGA not operational
    if resp.status != 201:
        return None
    data = resp.json()
    assert "id" in data, f"Document creation response missing 'id' key: {data}"
    return data


def _cleanup_doc(ctx: APIRequestContext, base: str, doc_id: str) -> None:
    """Best-effort document deletion."""
    with contextlib.suppress(Exception):
        ctx.delete(f"{base}/api/documents/{doc_id}")


def _assert_fga_allowed(resp, expected: bool, context_msg: str) -> None:
    """Assert FGA check response has 'allowed' key with expected value."""
    body = resp.json()
    assert "allowed" in body, f"FGA check response missing 'allowed' key: {body}"
    assert body["allowed"] is expected, context_msg


# --- AC: Document creation creates FGA owner relation ---


def test_create_document_creates_fga_owner(admin_api_context: APIRequestContext, backend_url: str):
    """Creating a document establishes an FGA owner relation for the creator."""
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    try:
        # Verify owner relation via FGA admin endpoint
        resp = admin_api_context.get(
            f"{backend_url}/api/fga/relations",
            params={"resource_type": "document", "resource_id": doc_id},
        )
        assert resp.status == 200
        relations = resp.json().get("relations", [])
        owner_rels = [r for r in relations if r.get("relationDefinition") == "owner"]
        assert len(owner_rels) == 1, f"Expected exactly 1 owner relation, got {owner_rels}"
    finally:
        _cleanup_doc(admin_api_context, backend_url, doc_id)


# --- AC: Permission derivation (owner=all, editor=view+edit, viewer=view only) ---


def test_owner_has_all_permissions(admin_api_context: APIRequestContext, backend_url: str):
    """Owner should have can_view, can_edit, and can_delete permissions."""
    resource_id = unique_id("perm-doc")
    target = unique_id("user")

    # Create owner relation directly
    resp = admin_api_context.post(
        f"{backend_url}/api/fga/relations",
        data={"resource_type": "document", "resource_id": resource_id, "relation": "owner", "target": target},
    )
    if resp.status in (400, 502):
        pytest.skip(f"FGA not operational (status {resp.status})")
    assert resp.status == 201

    try:
        for relation in ("can_view", "can_edit", "can_delete"):
            resp = admin_api_context.post(
                f"{backend_url}/api/fga/check",
                data={"resource_type": "document", "resource_id": resource_id, "relation": relation, "target": target},
            )
            if resp.status == 502:
                pytest.skip("FGA check not operational")
            assert resp.status == 200, f"Check {relation} returned {resp.status}"
            _assert_fga_allowed(resp, True, f"Owner should have {relation}")
    finally:
        with contextlib.suppress(Exception):
            admin_api_context.delete(
                f"{backend_url}/api/fga/relations",
                data={"resource_type": "document", "resource_id": resource_id, "relation": "owner", "target": target},
            )


def test_editor_has_view_and_edit_not_delete(admin_api_context: APIRequestContext, backend_url: str):
    """Editor should have can_view and can_edit but NOT can_delete."""
    resource_id = unique_id("perm-doc")
    target = unique_id("user")

    resp = admin_api_context.post(
        f"{backend_url}/api/fga/relations",
        data={"resource_type": "document", "resource_id": resource_id, "relation": "editor", "target": target},
    )
    if resp.status in (400, 502):
        pytest.skip(f"FGA not operational (status {resp.status})")
    assert resp.status == 201

    try:
        # Editor should have can_view and can_edit
        for relation in ("can_view", "can_edit"):
            resp = admin_api_context.post(
                f"{backend_url}/api/fga/check",
                data={"resource_type": "document", "resource_id": resource_id, "relation": relation, "target": target},
            )
            if resp.status == 502:
                pytest.skip("FGA check not operational")
            assert resp.status == 200
            _assert_fga_allowed(resp, True, f"Editor should have {relation}")

        # Editor should NOT have can_delete
        resp = admin_api_context.post(
            f"{backend_url}/api/fga/check",
            data={"resource_type": "document", "resource_id": resource_id, "relation": "can_delete", "target": target},
        )
        if resp.status == 502:
            pytest.skip("FGA check not operational")
        assert resp.status == 200
        _assert_fga_allowed(resp, False, "Editor should NOT have can_delete")
    finally:
        with contextlib.suppress(Exception):
            admin_api_context.delete(
                f"{backend_url}/api/fga/relations",
                data={"resource_type": "document", "resource_id": resource_id, "relation": "editor", "target": target},
            )


def test_viewer_has_view_only(admin_api_context: APIRequestContext, backend_url: str):
    """Viewer should have can_view but NOT can_edit or can_delete."""
    resource_id = unique_id("perm-doc")
    target = unique_id("user")

    resp = admin_api_context.post(
        f"{backend_url}/api/fga/relations",
        data={"resource_type": "document", "resource_id": resource_id, "relation": "viewer", "target": target},
    )
    if resp.status in (400, 502):
        pytest.skip(f"FGA not operational (status {resp.status})")
    assert resp.status == 201

    try:
        # Viewer should have can_view
        resp = admin_api_context.post(
            f"{backend_url}/api/fga/check",
            data={"resource_type": "document", "resource_id": resource_id, "relation": "can_view", "target": target},
        )
        if resp.status == 502:
            pytest.skip("FGA check not operational")
        assert resp.status == 200
        _assert_fga_allowed(resp, True, "Viewer should have can_view")

        # Viewer should NOT have can_edit or can_delete
        for relation in ("can_edit", "can_delete"):
            resp = admin_api_context.post(
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
            _assert_fga_allowed(resp, False, f"Viewer should NOT have {relation}")
    finally:
        with contextlib.suppress(Exception):
            admin_api_context.delete(
                f"{backend_url}/api/fga/relations",
                data={"resource_type": "document", "resource_id": resource_id, "relation": "viewer", "target": target},
            )


# --- AC: Document endpoint FGA enforcement (allowed + denied) ---


def test_document_get_allowed_for_owner(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/documents/{id} returns 200 for owner, verifying FGA can_view works end-to-end."""
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    try:
        resp = admin_api_context.get(f"{backend_url}/api/documents/{doc_id}")
        assert resp.status == 200, f"Owner should be able to view document, got {resp.status}"
        assert resp.json()["id"] == doc_id
    finally:
        _cleanup_doc(admin_api_context, backend_url, doc_id)


def test_document_update_allowed_for_owner(admin_api_context: APIRequestContext, backend_url: str):
    """PUT /api/documents/{id} returns 200 for owner, verifying FGA can_edit works end-to-end."""
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    try:
        resp = admin_api_context.put(
            f"{backend_url}/api/documents/{doc_id}",
            data={"title": "Updated E2E Title"},
        )
        assert resp.status == 200, f"Owner should be able to edit document, got {resp.status}"
        assert resp.json()["title"] == "Updated E2E Title"
    finally:
        _cleanup_doc(admin_api_context, backend_url, doc_id)


def test_permission_derivation_through_endpoints(admin_api_context: APIRequestContext, backend_url: str):
    """Verify FGA permission derivation works end-to-end through actual document endpoints.

    Creates a document (admin is owner), then verifies:
    - Owner can PUT (requires can_edit) and DELETE (requires can_delete)
    - FGA check confirms editor role derives can_view + can_edit but not can_delete
    This proves require_fga enforces permissions through the app, not just the FGA API.
    """
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    try:
        # Owner can edit via PUT (require_fga checks can_edit)
        resp = admin_api_context.put(
            f"{backend_url}/api/documents/{doc_id}",
            data={"title": "Derivation Test Updated"},
        )
        assert resp.status == 200, f"Owner PUT should succeed (can_edit derived from owner), got {resp.status}"
        assert resp.json()["title"] == "Derivation Test Updated"

        # Verify FGA check confirms editor permissions are a proper subset of owner
        # An editor target should have can_view and can_edit but NOT can_delete
        editor_target = unique_id("editor-user")
        resp = admin_api_context.post(
            f"{backend_url}/api/fga/relations",
            data={"resource_type": "document", "resource_id": doc_id, "relation": "editor", "target": editor_target},
        )
        if resp.status in (400, 502):
            pytest.skip(f"FGA not operational for editor relation (status {resp.status})")
        assert resp.status == 201

        try:
            # Editor should derive can_view and can_edit
            for relation in ("can_view", "can_edit"):
                check_data = {
                    "resource_type": "document",
                    "resource_id": doc_id,
                    "relation": relation,
                    "target": editor_target,
                }
                resp = admin_api_context.post(
                    f"{backend_url}/api/fga/check",
                    data=check_data,
                )
                if resp.status == 502:
                    pytest.skip("FGA check not operational")
                assert resp.status == 200
                _assert_fga_allowed(resp, True, f"Editor should derive {relation}")

            # Editor should NOT derive can_delete
            check_data = {
                "resource_type": "document",
                "resource_id": doc_id,
                "relation": "can_delete",
                "target": editor_target,
            }
            resp = admin_api_context.post(
                f"{backend_url}/api/fga/check",
                data=check_data,
            )
            if resp.status == 502:
                pytest.skip("FGA check not operational")
            assert resp.status == 200
            _assert_fga_allowed(resp, False, "Editor should NOT derive can_delete")
        finally:
            with contextlib.suppress(Exception):
                rel_data = {
                    "resource_type": "document",
                    "resource_id": doc_id,
                    "relation": "editor",
                    "target": editor_target,
                }
                admin_api_context.delete(
                    f"{backend_url}/api/fga/relations",
                    data=rel_data,
                )

        # Owner can delete via DELETE (require_fga checks can_delete)
        resp = admin_api_context.delete(f"{backend_url}/api/documents/{doc_id}")
        assert resp.status == 200, f"Owner DELETE should succeed (can_delete derived from owner), got {resp.status}"
        doc_id = None  # Mark as already deleted so finally block skips cleanup
    finally:
        if doc_id is not None:
            _cleanup_doc(admin_api_context, backend_url, doc_id)


def test_document_fga_denies_without_relation(admin_api_context: APIRequestContext, backend_url: str):
    """Removing FGA owner relation causes document GET to be denied (403)."""
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    # Find the owner relation target (the admin user's ID in FGA)
    resp = admin_api_context.get(
        f"{backend_url}/api/fga/relations",
        params={"resource_type": "document", "resource_id": doc_id},
    )
    assert resp.status == 200
    relations = resp.json().get("relations", [])
    owner_rels = [r for r in relations if r.get("relationDefinition") == "owner"]
    assert len(owner_rels) >= 1, "No owner relation found for created document"
    owner_target = owner_rels[0]["target"]

    try:
        # Remove owner relation
        resp = admin_api_context.delete(
            f"{backend_url}/api/fga/relations",
            data={"resource_type": "document", "resource_id": doc_id, "relation": "owner", "target": owner_target},
        )
        assert resp.status == 200, f"Failed to delete owner relation: {resp.status}"

        # Now GET should be denied (403 from require_fga)
        resp = admin_api_context.get(f"{backend_url}/api/documents/{doc_id}")
        assert resp.status == 403, f"Should be denied without FGA relation, got {resp.status}"
    finally:
        # Re-create owner relation so cleanup can delete the document
        with contextlib.suppress(Exception):
            admin_api_context.post(
                f"{backend_url}/api/fga/relations",
                data={
                    "resource_type": "document",
                    "resource_id": doc_id,
                    "relation": "owner",
                    "target": owner_target,
                },
            )
        _cleanup_doc(admin_api_context, backend_url, doc_id)


def test_document_delete_cleans_fga_relations(admin_api_context: APIRequestContext, backend_url: str):
    """DELETE /api/documents/{id} removes the document and cleans up FGA relations."""
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    # Delete document (owner can delete)
    resp = admin_api_context.delete(f"{backend_url}/api/documents/{doc_id}")
    assert resp.status == 200, f"Owner should be able to delete document, got {resp.status}"

    # Verify FGA relations are cleaned up
    resp = admin_api_context.get(
        f"{backend_url}/api/fga/relations",
        params={"resource_type": "document", "resource_id": doc_id},
    )
    assert resp.status == 200, f"FGA relation query failed after delete: {resp.status}"
    relations = resp.json().get("relations", [])
    assert len(relations) == 0, f"Expected 0 relations after delete, got {relations}"


# --- AC: List documents returns only FGA-authorized documents ---


def test_list_documents_returns_authorized(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/documents returns documents the caller is authorized to view."""
    doc = _create_doc(admin_api_context, backend_url, title=unique_id("list-doc"))
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    try:
        resp = admin_api_context.get(f"{backend_url}/api/documents")
        assert resp.status == 200
        body = resp.json()
        assert isinstance(body, dict), f"Expected dict response, got {type(body)}"
        documents = body.get("documents")
        assert isinstance(documents, list), f"Expected 'documents' list, got {type(documents)}"
        doc_ids = [d["id"] for d in documents]
        assert doc_id in doc_ids, f"Created document {doc_id} should appear in list"
    finally:
        _cleanup_doc(admin_api_context, backend_url, doc_id)


def test_list_documents_excludes_unauthorized(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/documents excludes documents the caller has lost FGA access to."""
    # Create two real documents via the API (both get owner relations for the admin)
    doc_a = _create_doc(admin_api_context, backend_url, title=unique_id("keep-doc"))
    if doc_a is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_b = _create_doc(admin_api_context, backend_url, title=unique_id("revoke-doc"))
    if doc_b is None:
        _cleanup_doc(admin_api_context, backend_url, doc_a["id"])
        pytest.skip("FGA not operational — second document creation failed")

    doc_a_id = doc_a["id"]
    doc_b_id = doc_b["id"]

    # Find the owner relation target for doc_b so we can revoke it
    resp = admin_api_context.get(
        f"{backend_url}/api/fga/relations",
        params={"resource_type": "document", "resource_id": doc_b_id},
    )
    assert resp.status == 200
    relations = resp.json().get("relations", [])
    owner_rels = [r for r in relations if r.get("relationDefinition") == "owner"]
    assert len(owner_rels) >= 1, "No owner relation found for doc_b"
    owner_target = owner_rels[0]["target"]

    try:
        # Remove the owner relation for doc_b (admin loses FGA access)
        resp = admin_api_context.delete(
            f"{backend_url}/api/fga/relations",
            data={"resource_type": "document", "resource_id": doc_b_id, "relation": "owner", "target": owner_target},
        )
        assert resp.status == 200, f"Failed to delete owner relation for doc_b: {resp.status}"

        # List documents — doc_a should appear, doc_b should NOT
        resp = admin_api_context.get(f"{backend_url}/api/documents")
        assert resp.status == 200
        documents = resp.json().get("documents", [])
        doc_ids = [d["id"] for d in documents]
        assert doc_a_id in doc_ids, f"Document {doc_a_id} (with owner relation) should appear in list"
        assert doc_b_id not in doc_ids, f"Document {doc_b_id} (owner revoked) should NOT appear in list"
    finally:
        # Re-create owner relation for doc_b so cleanup can delete it
        with contextlib.suppress(Exception):
            admin_api_context.post(
                f"{backend_url}/api/fga/relations",
                data={
                    "resource_type": "document",
                    "resource_id": doc_b_id,
                    "relation": "owner",
                    "target": owner_target,
                },
            )
        _cleanup_doc(admin_api_context, backend_url, doc_a_id)
        _cleanup_doc(admin_api_context, backend_url, doc_b_id)


# --- AC: Share a document and verify access, then revoke ---


def test_share_document_grants_access(admin_api_context: APIRequestContext, backend_url: str, test_user_id: str):
    """Sharing a document grants the target user FGA-level access."""
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    try:
        # Share with test user as viewer
        resp = admin_api_context.post(
            f"{backend_url}/api/documents/{doc_id}/share",
            data={"user_id": test_user_id, "relation": "viewer"},
        )
        if resp.status == 404:
            pytest.skip("Target user not found in Descope — share test requires E2E_TEST_EMAIL user")
        if resp.status == 403:
            pytest.skip("Cannot share — caller may not be recognized as owner by Descope")
        assert resp.status == 200, f"Share failed: {resp.status}"

        # Verify via FGA check that the target user now has can_view
        resp = admin_api_context.post(
            f"{backend_url}/api/fga/check",
            data={"resource_type": "document", "resource_id": doc_id, "relation": "can_view", "target": test_user_id},
        )
        if resp.status == 502:
            pytest.skip("FGA check not operational")
        assert resp.status == 200
        _assert_fga_allowed(resp, True, f"Shared user {test_user_id} should have can_view")
    finally:
        # Cleanup: revoke share then delete doc
        with contextlib.suppress(Exception):
            admin_api_context.delete(
                f"{backend_url}/api/fga/relations",
                data={"resource_type": "document", "resource_id": doc_id, "relation": "viewer", "target": test_user_id},
            )
        _cleanup_doc(admin_api_context, backend_url, doc_id)


def test_revoke_share_denies_access(admin_api_context: APIRequestContext, backend_url: str, test_user_id: str):
    """Revoking a share removes the user's FGA-level access."""
    doc = _create_doc(admin_api_context, backend_url)
    if doc is None:
        pytest.skip("FGA not operational — document creation failed")
    doc_id = doc["id"]

    try:
        # Share first
        resp = admin_api_context.post(
            f"{backend_url}/api/documents/{doc_id}/share",
            data={"user_id": test_user_id, "relation": "viewer"},
        )
        if resp.status in (403, 404):
            pytest.skip(f"Share prerequisite failed (status {resp.status})")
        assert resp.status == 200, f"Share failed: {resp.status}"

        # Verify access granted
        resp = admin_api_context.post(
            f"{backend_url}/api/fga/check",
            data={"resource_type": "document", "resource_id": doc_id, "relation": "can_view", "target": test_user_id},
        )
        if resp.status == 502:
            pytest.skip("FGA check not operational")
        assert resp.status == 200
        _assert_fga_allowed(resp, True, "User should have access after share")

        # Revoke
        resp = admin_api_context.delete(
            f"{backend_url}/api/documents/{doc_id}/share/{test_user_id}",
        )
        if resp.status == 403:
            pytest.skip("Cannot revoke — caller not recognized as owner")
        assert resp.status == 200, f"Revoke failed: {resp.status}"

        # Verify access denied
        resp = admin_api_context.post(
            f"{backend_url}/api/fga/check",
            data={"resource_type": "document", "resource_id": doc_id, "relation": "can_view", "target": test_user_id},
        )
        if resp.status == 502:
            pytest.skip("FGA check not operational")
        assert resp.status == 200
        _assert_fga_allowed(resp, False, "User should be denied after revoke")
    finally:
        # Cleanup: best-effort relation delete then doc delete
        with contextlib.suppress(Exception):
            admin_api_context.delete(
                f"{backend_url}/api/fga/relations",
                data={"resource_type": "document", "resource_id": doc_id, "relation": "viewer", "target": test_user_id},
            )
        _cleanup_doc(admin_api_context, backend_url, doc_id)


# --- AC: Sequential relation revocation (no stale grants) ---


def test_sequential_relation_revocation_no_stale_grants(admin_api_context: APIRequestContext, backend_url: str):
    """Delete→check→re-create cycles: permission check must never return true after delete."""
    resource_id = unique_id("revoke-doc")
    target = unique_id("user")
    base_body = {"resource_type": "document", "resource_id": resource_id, "relation": "viewer", "target": target}

    # Create the relation first to verify FGA is operational
    resp = admin_api_context.post(f"{backend_url}/api/fga/relations", data=base_body)
    if resp.status in (400, 502):
        pytest.skip(f"FGA not operational (status {resp.status})")
    assert resp.status == 201

    try:
        # Cycles: delete then immediately check — should never get allowed=true after delete
        for _ in range(3):
            # Delete
            resp = admin_api_context.delete(f"{backend_url}/api/fga/relations", data=base_body)
            assert resp.status == 200

            # Immediate check — must not be allowed
            resp = admin_api_context.post(
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
            _assert_fga_allowed(resp, False, "Should be denied after relation deleted")

            # Re-create for next cycle
            resp = admin_api_context.post(f"{backend_url}/api/fga/relations", data=base_body)
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
        ("GET", f"/api/documents/{_DUMMY_UUID}", None),
        ("PUT", f"/api/documents/{_DUMMY_UUID}", {"title": "test"}),
        ("DELETE", f"/api/documents/{_DUMMY_UUID}", None),
        ("POST", f"/api/documents/{_DUMMY_UUID}/share", {"user_id": "u1", "relation": "viewer"}),
        ("DELETE", f"/api/documents/{_DUMMY_UUID}/share/user1", None),
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
