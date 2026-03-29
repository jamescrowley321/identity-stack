"""E2E tests for FGA administration: schema, relations, and permission checks.

These tests require DESCOPE_MANAGEMENT_KEY env var (for admin token via tenant-scoped access key).
They exercise the real Descope FGA API via the backend's /api/fga/* endpoints.
"""

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
    """Generate a unique ID for test resources."""
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


# --- AC-1: Schema retrieval ---


def test_get_fga_schema(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/fga/schema returns current schema (may be empty for fresh project)."""
    resp, elapsed_ms = _timed_request(admin_api_context, "GET", f"{backend_url}/api/fga/schema")
    assert resp.status == 200, f"Expected 200, got {resp.status}"
    assert elapsed_ms < MAX_API_RESPONSE_MS

    body = resp.json()
    assert "schema" in body
    assert isinstance(body["schema"], str)


# --- AC-3: Create and delete relation ---


def test_relation_create_and_delete(admin_api_context: APIRequestContext, backend_url: str):
    """Relation can be created and deleted via API."""
    resource_id = _unique_id("doc")
    relation_body = {
        "resource_type": "document",
        "resource_id": resource_id,
        "relation": "owner",
        "target": f"user:{_unique_id('u')}",
    }

    # Create relation
    resp, elapsed_ms = _timed_request(
        admin_api_context,
        "POST",
        f"{backend_url}/api/fga/relations",
        data=relation_body,
    )
    # May return 201 (success) or 400 (schema not configured) depending on FGA state
    if resp.status == 400:
        pytest.skip("FGA schema not configured in this Descope project")
    assert resp.status == 201, f"Create failed: {resp.status}"
    assert elapsed_ms < MAX_API_RESPONSE_MS

    # Verify via list
    resp, elapsed_ms = _timed_request(
        admin_api_context,
        "GET",
        f"{backend_url}/api/fga/relations",
        params={"resource_type": "document", "resource_id": resource_id},
    )
    assert resp.status == 200
    assert elapsed_ms < MAX_API_RESPONSE_MS
    relations = resp.json().get("relations", [])
    assert any(r["target"] == relation_body["target"] for r in relations), "Created relation not found in list"

    # Delete relation
    resp, elapsed_ms = _timed_request(
        admin_api_context,
        "DELETE",
        f"{backend_url}/api/fga/relations",
        data=relation_body,
    )
    assert resp.status == 200, f"Delete failed: {resp.status}"
    assert elapsed_ms < MAX_API_RESPONSE_MS


# --- AC-2: List relations ---


def test_list_relations_empty(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/fga/relations returns empty list for non-existent resource."""
    resp, elapsed_ms = _timed_request(
        admin_api_context,
        "GET",
        f"{backend_url}/api/fga/relations",
        params={"resource_type": "nonexistent", "resource_id": "nope-000"},
    )
    assert resp.status == 200
    assert elapsed_ms < MAX_API_RESPONSE_MS
    body = resp.json()
    assert body["relations"] == []


# --- AC-5: Authorization check ---


def test_check_permission_denied_for_nonexistent(admin_api_context: APIRequestContext, backend_url: str):
    """POST /api/fga/check returns denied for a resource with no relations."""
    resp, elapsed_ms = _timed_request(
        admin_api_context,
        "POST",
        f"{backend_url}/api/fga/check",
        data={
            "resource_type": "document",
            "resource_id": _unique_id("nonexist"),
            "relation": "viewer",
            "target": "user:nobody",
        },
    )
    # May return 200 (allowed: false) or 502 (FGA not configured) depending on project state
    if resp.status == 502:
        pytest.skip("FGA not configured in this Descope project")
    assert resp.status == 200, f"Expected 200, got {resp.status}"
    assert elapsed_ms < MAX_API_RESPONSE_MS
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
    resp, _ = _timed_request(
        admin_api_context,
        "POST",
        f"{backend_url}/api/fga/relations",
        data={"resource_type": "", "resource_id": "1", "relation": "owner", "target": "u1"},
    )
    assert resp.status == 422, f"Expected 422 for empty resource_type, got {resp.status}"


def test_list_relations_validation(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/fga/relations rejects missing query params."""
    resp, _ = _timed_request(
        admin_api_context,
        "GET",
        f"{backend_url}/api/fga/relations",
    )
    assert resp.status == 422, f"Expected 422 for missing params, got {resp.status}"


def test_check_permission_validation(admin_api_context: APIRequestContext, backend_url: str):
    """POST /api/fga/check rejects incomplete payload."""
    resp, _ = _timed_request(
        admin_api_context,
        "POST",
        f"{backend_url}/api/fga/check",
        data={"resource_type": "doc", "resource_id": "1"},
    )
    assert resp.status == 422, f"Expected 422 for missing fields, got {resp.status}"
