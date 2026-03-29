"""E2E tests for RBAC administration: TF seed verification and CRUD lifecycle.

These tests require DESCOPE_MANAGEMENT_KEY env var (for admin token via tenant-scoped access key).
They exercise the real Descope API via the backend's /api/roles and /api/permissions endpoints.
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

# --- Constants (source: infra/rbac.tf) ---

MAX_API_RESPONSE_MS = 2000

TF_SEEDED_PERMISSIONS = sorted(
    [
        "projects.create",
        "projects.read",
        "projects.update",
        "projects.delete",
        "members.invite",
        "members.remove",
        "members.update_role",
        "documents.read",
        "documents.write",
        "documents.delete",
        "settings.manage",
        "billing.manage",
    ]
)

TF_SEEDED_ROLES = {
    "owner": 12,
    "admin": 11,
    "member": 5,
    "viewer": 2,
}


def _unique_name(prefix: str) -> str:
    """Generate a unique name for test resources to avoid collisions."""
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
    return resp, elapsed_ms


# --- AC-1: TF-seeded roles with correct permission counts ---


def test_tf_seeded_roles_present(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/roles returns all 4 TF-seeded roles with correct permission counts."""
    resp, elapsed_ms = _timed_request(admin_api_context, "GET", f"{backend_url}/api/roles")
    assert resp.status == 200, f"Expected 200, got {resp.status}"
    assert elapsed_ms < MAX_API_RESPONSE_MS, f"Response took {elapsed_ms:.0f}ms, limit is {MAX_API_RESPONSE_MS}ms"

    body = resp.json()
    roles = body.get("roles", [])
    role_map = {r["name"]: r for r in roles}

    for role_name, expected_perm_count in TF_SEEDED_ROLES.items():
        assert role_name in role_map, f"TF-seeded role '{role_name}' not found in response"
        actual_count = len(role_map[role_name].get("permissionNames") or [])
        assert actual_count == expected_perm_count, (
            f"Role '{role_name}': expected {expected_perm_count} permissions, got {actual_count}"
        )


# --- AC-2: TF-seeded permissions ---


def test_tf_seeded_permissions_present(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/permissions returns all 12 TF-seeded permissions."""
    resp, elapsed_ms = _timed_request(admin_api_context, "GET", f"{backend_url}/api/permissions")
    assert resp.status == 200, f"Expected 200, got {resp.status}"
    assert elapsed_ms < MAX_API_RESPONSE_MS, f"Response took {elapsed_ms:.0f}ms, limit is {MAX_API_RESPONSE_MS}ms"

    body = resp.json()
    permissions = body.get("permissions", [])
    perm_names = sorted([p["name"] for p in permissions])

    for expected in TF_SEEDED_PERMISSIONS:
        assert expected in perm_names, f"TF-seeded permission '{expected}' not found in response"


# --- AC-3: Runtime-created coexist with TF-seeded ---


def test_runtime_permission_coexists_with_tf_seeded(admin_api_context: APIRequestContext, backend_url: str):
    """Runtime-created permission appears alongside TF-seeded permissions."""
    perm_name = _unique_name("test-perm")
    try:
        # Create runtime permission
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/permissions",
            data={"name": perm_name, "description": "E2E test permission"},
        )
        assert resp.status == 201, f"Create failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        # List and verify coexistence
        resp, elapsed_ms = _timed_request(admin_api_context, "GET", f"{backend_url}/api/permissions")
        assert resp.status == 200
        assert elapsed_ms < MAX_API_RESPONSE_MS

        perm_names = [p["name"] for p in resp.json().get("permissions", [])]
        assert perm_name in perm_names, "Runtime permission not found"
        for tf_perm in TF_SEEDED_PERMISSIONS:
            assert tf_perm in perm_names, f"TF-seeded permission '{tf_perm}' missing after runtime create"
    finally:
        with contextlib.suppress(Exception):
            admin_api_context.delete(f"{backend_url}/api/permissions/{perm_name}")


def test_runtime_role_coexists_with_tf_seeded(admin_api_context: APIRequestContext, backend_url: str):
    """Runtime-created role appears alongside TF-seeded roles."""
    role_name = _unique_name("test-role")
    try:
        # Create runtime role
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles",
            data={"name": role_name, "description": "E2E test role", "permission_names": ["projects.read"]},
        )
        assert resp.status == 201, f"Create failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        # List and verify coexistence
        resp, elapsed_ms = _timed_request(admin_api_context, "GET", f"{backend_url}/api/roles")
        assert resp.status == 200
        assert elapsed_ms < MAX_API_RESPONSE_MS

        role_names = [r["name"] for r in resp.json().get("roles", [])]
        assert role_name in role_names, "Runtime role not found"
        for tf_role in TF_SEEDED_ROLES:
            assert tf_role in role_names, f"TF-seeded role '{tf_role}' missing after runtime create"
    finally:
        with contextlib.suppress(Exception):
            admin_api_context.delete(f"{backend_url}/api/roles/{role_name}")


# --- AC-4: Permission CRUD lifecycle ---


def test_permission_crud_lifecycle(admin_api_context: APIRequestContext, backend_url: str):
    """Full permission lifecycle: list -> create -> update -> delete -> verify removed."""
    perm_name = _unique_name("lifecycle-perm")
    updated_name = perm_name + "-updated"
    cleanup_name = perm_name  # tracks current name for cleanup

    try:
        # List (baseline)
        resp, elapsed_ms = _timed_request(admin_api_context, "GET", f"{backend_url}/api/permissions")
        assert resp.status == 200
        assert elapsed_ms < MAX_API_RESPONSE_MS
        baseline_names = [p["name"] for p in resp.json().get("permissions", [])]
        assert perm_name not in baseline_names

        # Create
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/permissions",
            data={"name": perm_name, "description": "E2E lifecycle test"},
        )
        assert resp.status == 201, f"Create failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        # Update
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "PUT",
            f"{backend_url}/api/permissions/{perm_name}",
            data={"new_name": updated_name, "description": "Updated description"},
        )
        assert resp.status == 200, f"Update failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        cleanup_name = updated_name

        # Verify update in list
        resp, _ = _timed_request(admin_api_context, "GET", f"{backend_url}/api/permissions")
        perm_names = [p["name"] for p in resp.json().get("permissions", [])]
        assert updated_name in perm_names, "Updated permission name not found"
        assert perm_name not in perm_names, "Old permission name still present"

        # Delete
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "DELETE",
            f"{backend_url}/api/permissions/{updated_name}",
        )
        assert resp.status == 200, f"Delete failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        cleanup_name = None  # already deleted

        # Verify removed
        resp, _ = _timed_request(admin_api_context, "GET", f"{backend_url}/api/permissions")
        perm_names = [p["name"] for p in resp.json().get("permissions", [])]
        assert updated_name not in perm_names, "Deleted permission still present"
    finally:
        if cleanup_name:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/permissions/{cleanup_name}")


# --- AC-5: Role CRUD with permission mapping ---


def test_role_crud_lifecycle(admin_api_context: APIRequestContext, backend_url: str):
    """Full role lifecycle: list -> create (with permissions) -> update mapping -> delete -> verify removed."""
    role_name = _unique_name("lifecycle-role")
    updated_name = role_name + "-updated"
    cleanup_name = role_name

    try:
        # List (baseline)
        resp, elapsed_ms = _timed_request(admin_api_context, "GET", f"{backend_url}/api/roles")
        assert resp.status == 200
        assert elapsed_ms < MAX_API_RESPONSE_MS
        baseline_names = [r["name"] for r in resp.json().get("roles", [])]
        assert role_name not in baseline_names

        # Create with permissions
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles",
            data={
                "name": role_name,
                "description": "E2E lifecycle test",
                "permission_names": ["projects.read", "documents.read"],
            },
        )
        assert resp.status == 201, f"Create failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        # Verify in list with permissions
        resp, _ = _timed_request(admin_api_context, "GET", f"{backend_url}/api/roles")
        roles = resp.json().get("roles", [])
        created = next((r for r in roles if r["name"] == role_name), None)
        assert created is not None, "Created role not found in list"
        created_perms = sorted(created.get("permissionNames") or [])
        assert created_perms == ["documents.read", "projects.read"], f"Unexpected permissions: {created_perms}"

        # Update permission mapping (add settings.manage, remove documents.read)
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "PUT",
            f"{backend_url}/api/roles/{role_name}",
            data={
                "new_name": updated_name,
                "description": "Updated lifecycle test",
                "permission_names": ["projects.read", "settings.manage"],
            },
        )
        assert resp.status == 200, f"Update failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        cleanup_name = updated_name

        # Verify updated mapping
        resp, _ = _timed_request(admin_api_context, "GET", f"{backend_url}/api/roles")
        roles = resp.json().get("roles", [])
        updated = next((r for r in roles if r["name"] == updated_name), None)
        assert updated is not None, "Updated role not found"
        updated_perms = sorted(updated.get("permissionNames") or [])
        assert updated_perms == ["projects.read", "settings.manage"], f"Unexpected permissions: {updated_perms}"

        # Delete
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "DELETE",
            f"{backend_url}/api/roles/{updated_name}",
        )
        assert resp.status == 200, f"Delete failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        cleanup_name = None

        # Verify removed
        resp, _ = _timed_request(admin_api_context, "GET", f"{backend_url}/api/roles")
        role_names = [r["name"] for r in resp.json().get("roles", [])]
        assert updated_name not in role_names, "Deleted role still present"
    finally:
        if cleanup_name:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{cleanup_name}")


# --- AC-6: Runtime role works with /roles/assign ---


def test_runtime_role_assignment(admin_api_context: APIRequestContext, backend_url: str):
    """Runtime-created role can be assigned to a user via /roles/assign."""
    role_name = _unique_name("assign-role")
    cleanup_role = False

    try:
        # Create a runtime role
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles",
            data={"name": role_name, "description": "E2E assignment test"},
        )
        assert resp.status == 201, f"Create failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        cleanup_role = True

        # Get current user info to find user_id and tenant_id
        resp, _ = _timed_request(admin_api_context, "GET", f"{backend_url}/api/me")
        assert resp.status == 200
        me_data = resp.json()
        user_id = (me_data.get("identity") or {}).get("sub", "")
        assert user_id, "Could not determine user_id from /api/me"

        resp, _ = _timed_request(admin_api_context, "GET", f"{backend_url}/api/tenants")
        assert resp.status == 200
        tenants = resp.json().get("tenants", {})
        assert isinstance(tenants, dict), f"Expected tenants dict, got {type(tenants).__name__}"
        tenant_id = next(iter(tenants), "")
        assert tenant_id, "Could not determine tenant_id from /api/tenants"

        # Assign the runtime role
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles/assign",
            data={"user_id": user_id, "tenant_id": tenant_id, "role_names": [role_name]},
        )
        assert resp.status == 200, f"Assign failed: {resp.status} — {resp.text()}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        # Verify assignment took effect by checking user's roles
        resp, _ = _timed_request(admin_api_context, "GET", f"{backend_url}/api/roles/user/{user_id}")
        if resp.status == 200:
            user_roles = [r["name"] for r in resp.json().get("roles", [])]
            assert role_name in user_roles, f"Assigned role '{role_name}' not found in user roles"

        # Remove the assigned role (cleanup user state)
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles/remove",
            data={"user_id": user_id, "tenant_id": tenant_id, "role_names": [role_name]},
        )
        assert resp.status == 200, f"Remove failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
    finally:
        if cleanup_role:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{role_name}")


# --- AC-8: Protected endpoints require auth ---


def test_rbac_endpoints_reject_no_auth(api_context: APIRequestContext, backend_url: str):
    """RBAC admin endpoints return 401 without auth."""
    dummy_payload = {"name": "test", "description": "test"}
    endpoints = [
        ("GET", "/api/permissions"),
        ("GET", "/api/roles"),
        ("POST", "/api/permissions"),
        ("POST", "/api/roles"),
        ("PUT", "/api/permissions/nonexistent"),
        ("PUT", "/api/roles/nonexistent"),
        ("DELETE", "/api/permissions/nonexistent"),
        ("DELETE", "/api/roles/nonexistent"),
    ]
    for method, path in endpoints:
        url = f"{backend_url}{path}"
        if method == "GET":
            resp = api_context.get(url)
        elif method == "POST":
            resp = api_context.post(url, data=dummy_payload)
        elif method == "PUT":
            resp = api_context.put(url, data=dummy_payload)
        elif method == "DELETE":
            resp = api_context.delete(url)
        else:
            continue
        assert resp.status == 401, f"{method} {path} returned {resp.status}, expected 401"
