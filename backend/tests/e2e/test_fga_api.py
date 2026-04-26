"""E2E tests for FGA administration: schema, relations, and permission checks.

These tests require DESCOPE_MANAGEMENT_KEY env var (for admin token via tenant-scoped access key).
They exercise the real Descope FGA API via the backend's /api/fga/* endpoints.
"""

import contextlib
import json
import os

import pytest
from playwright.sync_api import APIRequestContext

from tests.e2e.helpers.api import unique_id

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY"),
    reason="DESCOPE_MANAGEMENT_KEY not set",
)


# --- AC-1: Schema retrieval ---


def test_get_fga_schema(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/fga/schema returns current schema (may be empty for fresh project)."""
    resp = admin_api_context.get(f"{backend_url}/api/fga/schema")
    assert resp.status == 200, f"Expected 200, got {resp.status}"

    body = resp.json()
    assert "schema" in body
    # Descope returns schema as structured dict (live API) or str (serialized)
    assert isinstance(body["schema"], (str, dict))


# --- AC-3: Create and delete relation ---


def test_relation_create_and_delete(admin_api_context: APIRequestContext, backend_url: str):
    """Relation can be created and deleted via API."""
    resource_id = unique_id("doc")
    relation_body = {
        "resource_type": "document",
        "resource_id": resource_id,
        "relation": "owner",
        "target": f"user:{unique_id('u')}",
    }

    # Create relation
    resp = admin_api_context.post(
        f"{backend_url}/api/fga/relations",
        data=relation_body,
    )
    # May return 201 (success), 400 (schema not configured), or 502 (Descope API error)
    if resp.status in (400, 502):
        pytest.skip(f"FGA not operational in this Descope project (status {resp.status})")
    assert resp.status == 201, f"Create failed: {resp.status}"

    # Verify via list
    resp = admin_api_context.get(
        f"{backend_url}/api/fga/relations",
        params={"resource_type": "document", "resource_id": resource_id},
    )
    assert resp.status == 200
    relations = resp.json().get("relations", [])
    assert any(r["target"] == relation_body["target"] for r in relations), "Created relation not found in list"

    # Delete relation
    resp = admin_api_context.delete(
        f"{backend_url}/api/fga/relations",
        data=relation_body,
    )
    assert resp.status == 200, f"Delete failed: {resp.status}"


# --- AC-2: List relations ---


def test_list_relations_empty(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/fga/relations returns empty list or 400 for non-existent resource."""
    resp = admin_api_context.get(
        f"{backend_url}/api/fga/relations",
        params={"resource_type": "nonexistent", "resource_id": "nope-000"},
    )
    # Descope API may return 400 when resource_type is unknown or required fields are missing
    if resp.status == 400:
        pytest.skip("Descope API rejected request for unknown resource type")
    assert resp.status == 200
    body = resp.json()
    assert body["relations"] == []


# --- AC-5: Authorization check ---


def test_check_permission_denied_for_nonexistent(admin_api_context: APIRequestContext, backend_url: str):
    """POST /api/fga/check returns denied for a resource with no relations."""
    resp = admin_api_context.post(
        f"{backend_url}/api/fga/check",
        data={
            "resource_type": "document",
            "resource_id": unique_id("nonexist"),
            "relation": "viewer",
            "target": "user:nobody",
        },
    )
    # May return 200 (allowed: false) or 502 (FGA not configured) depending on project state
    if resp.status == 502:
        pytest.skip("FGA not configured in this Descope project")
    assert resp.status == 200, f"Expected 200, got {resp.status}"
    assert resp.json()["allowed"] is False


# --- AC-6: Non-admin rejected ---


def test_fga_endpoints_reject_no_auth(api_context: APIRequestContext, backend_url: str):
    """FGA admin endpoints return 401 without auth."""
    dummy_payload = {"resource_type": "doc", "resource_id": "1", "relation": "owner", "target": "u1"}
    endpoints = [
        ("GET", "/api/fga/schema"),
        ("PUT", "/api/fga/schema"),
        ("GET", "/api/fga/relations"),
        ("POST", "/api/fga/relations"),
        ("DELETE", "/api/fga/relations"),
        ("POST", "/api/fga/check"),
    ]
    for method, path in endpoints:
        url = f"{backend_url}{path}"
        if method == "GET":
            if "relations" in path:
                resp = api_context.get(url, params={"resource_type": "doc", "resource_id": "1"})
            else:
                resp = api_context.get(url)
        elif method == "POST":
            resp = api_context.post(url, data=dummy_payload)
        elif method == "PUT":
            resp = api_context.put(url, data={"schema": "test"})
        elif method == "DELETE":
            resp = api_context.delete(url, data=dummy_payload)
        else:
            continue
        assert resp.status == 401, f"{method} {path} returned {resp.status}, expected 401"


# --- Validation: missing/empty fields rejected ---


def test_create_relation_validation(admin_api_context: APIRequestContext, backend_url: str):
    """POST /api/fga/relations rejects empty fields."""
    resp = admin_api_context.post(
        f"{backend_url}/api/fga/relations",
        data={"resource_type": "", "resource_id": "1", "relation": "owner", "target": "u1"},
    )
    assert resp.status == 422, f"Expected 422 for empty resource_type, got {resp.status}"


def test_list_relations_validation(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/fga/relations rejects missing query params."""
    resp = admin_api_context.get(f"{backend_url}/api/fga/relations")
    assert resp.status == 422, f"Expected 422 for missing params, got {resp.status}"


def test_check_permission_validation(admin_api_context: APIRequestContext, backend_url: str):
    """POST /api/fga/check rejects incomplete payload."""
    resp = admin_api_context.post(
        f"{backend_url}/api/fga/check",
        data={"resource_type": "doc", "resource_id": "1"},
    )
    assert resp.status == 422, f"Expected 422 for missing fields, got {resp.status}"


# --- Relation lifecycle with authorization check ---


def test_relation_lifecycle_with_permission_check(admin_api_context: APIRequestContext, backend_url: str):
    """Create relation → verify allowed → delete → verify denied (full lifecycle)."""
    resource_id = unique_id("lifecycle")
    target = f"user:{unique_id('u')}"
    relation_body = {
        "resource_type": "document",
        "resource_id": resource_id,
        "relation": "viewer",
        "target": target,
    }

    # Create
    resp = admin_api_context.post(f"{backend_url}/api/fga/relations", data=relation_body)
    if resp.status in (400, 502):
        pytest.skip(f"FGA not operational (status {resp.status})")
    assert resp.status == 201

    try:
        # Check — should be allowed (viewer → can_view)
        check_body = {
            "resource_type": "document",
            "resource_id": resource_id,
            "relation": "can_view",
            "target": target,
        }
        resp = admin_api_context.post(f"{backend_url}/api/fga/check", data=check_body)
        if resp.status == 502:
            pytest.skip("FGA check not operational")
        assert resp.status == 200
        body = resp.json()
        assert "allowed" in body, f"FGA check response missing 'allowed' key: {body}"
        assert body["allowed"] is True, "Viewer should have can_view"

        # Delete
        resp = admin_api_context.delete(f"{backend_url}/api/fga/relations", data=relation_body)
        assert resp.status == 200

        # Check again — should be denied
        resp = admin_api_context.post(f"{backend_url}/api/fga/check", data=check_body)
        if resp.status == 502:
            pytest.skip("FGA check not operational")
        assert resp.status == 200
        body = resp.json()
        assert "allowed" in body, f"FGA check response missing 'allowed' key: {body}"
        assert body["allowed"] is False, "Should be denied after relation deleted"
    finally:
        # Best-effort cleanup
        with contextlib.suppress(Exception):
            admin_api_context.delete(f"{backend_url}/api/fga/relations", data=relation_body)


# --- Schema update (PUT) ---


def test_update_fga_schema(admin_api_context: APIRequestContext, backend_url: str):
    """PUT /api/fga/schema updates the schema and returns the result."""
    # Read current schema first
    resp = admin_api_context.get(f"{backend_url}/api/fga/schema")
    assert resp.status == 200
    original = resp.json().get("schema", "")

    if not original:
        pytest.skip("No FGA schema configured — cannot test update")

    # Re-save the same schema (idempotent — safe for concurrent runs)
    schema_str = original if isinstance(original, str) else json.dumps(original)
    resp = admin_api_context.put(
        f"{backend_url}/api/fga/schema",
        data={"schema": schema_str},
    )
    if resp.status in (400, 502):
        pytest.skip(f"Schema update not supported (status {resp.status})")
    assert resp.status == 200
    assert "schema" in resp.json()
