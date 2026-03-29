"""E2E tests for RBAC administration: CRUD lifecycle and auth enforcement.

These tests require DESCOPE_MANAGEMENT_KEY env var (for admin token via tenant-scoped access key).
They exercise the real Descope API via the backend's /api/roles and /api/permissions endpoints.

NOTE: Descope's /v1/mgmt/role/all and /v1/mgmt/permission/all endpoints return 500 (E010009)
for this project. Tests that require listing are marked xfail. CRUD tests verify operations
via create → duplicate-conflict → update → delete → re-create cycle instead.
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

# --- Constants ---

MAX_API_RESPONSE_MS = 2000

# TF-seeded role/permission names (source: infra/rbac.tf)
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
    if not (200 <= resp.status < 300):
        print(f"[E2E] {method} {url} → {resp.status}: {resp.text()}")
    return resp, elapsed_ms


# --- AC-1: TF-seeded roles (requires listing — Descope returns 500) ---


@pytest.mark.xfail(reason="Descope API returns 500 (E010009) on /v1/mgmt/role/all", strict=False)
def test_tf_seeded_roles_present(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/roles returns all 4 TF-seeded roles with correct permission counts."""
    resp, elapsed_ms = _timed_request(admin_api_context, "GET", f"{backend_url}/api/roles")
    assert resp.status == 200, f"Expected 200, got {resp.status}"
    assert elapsed_ms < MAX_API_RESPONSE_MS

    body = resp.json()
    roles = body.get("roles", [])
    role_map = {r["name"]: r for r in roles}

    for role_name, expected_perm_count in TF_SEEDED_ROLES.items():
        assert role_name in role_map, f"TF-seeded role '{role_name}' not found in response"
        actual_count = len(role_map[role_name].get("permissionNames") or [])
        assert actual_count == expected_perm_count, (
            f"Role '{role_name}': expected {expected_perm_count} permissions, got {actual_count}"
        )


# --- AC-2: TF-seeded permissions (requires listing — Descope returns 500) ---


@pytest.mark.xfail(reason="Descope API returns 500 (E010009) on /v1/mgmt/permission/all", strict=False)
def test_tf_seeded_permissions_present(admin_api_context: APIRequestContext, backend_url: str):
    """GET /api/permissions returns all 12 TF-seeded permissions."""
    resp, elapsed_ms = _timed_request(admin_api_context, "GET", f"{backend_url}/api/permissions")
    assert resp.status == 200, f"Expected 200, got {resp.status}"
    assert elapsed_ms < MAX_API_RESPONSE_MS

    body = resp.json()
    permissions = body.get("permissions", [])
    perm_names = sorted([p["name"] for p in permissions])

    for expected in TF_SEEDED_PERMISSIONS:
        assert expected in perm_names, f"TF-seeded permission '{expected}' not found in response"


# --- AC-3: Runtime-created resources can be created and deleted ---


def test_runtime_permission_create_and_delete(admin_api_context: APIRequestContext, backend_url: str):
    """Runtime-created permission can be created and deleted via API."""
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

        # Verify it exists by trying to create duplicate (expect 409 or 400)
        resp, _ = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/permissions",
            data={"name": perm_name, "description": "duplicate"},
        )
        assert resp.status in (400, 409), f"Duplicate create should fail, got {resp.status}"

        # Delete
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "DELETE",
            f"{backend_url}/api/permissions/{perm_name}",
        )
        assert resp.status == 200, f"Delete failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        perm_name = None  # cleaned up
    finally:
        if perm_name:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/permissions/{perm_name}")


def test_runtime_role_create_and_delete(admin_api_context: APIRequestContext, backend_url: str):
    """Runtime-created role can be created and deleted via API."""
    role_name = _unique_name("test-role")
    try:
        # Create runtime role (no permission_names since TF-seeded perms may not exist)
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles",
            data={"name": role_name, "description": "E2E test role"},
        )
        assert resp.status == 201, f"Create failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        # Verify it exists by trying to create duplicate (expect 409 or 400)
        resp, _ = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles",
            data={"name": role_name, "description": "duplicate"},
        )
        assert resp.status in (400, 409), f"Duplicate create should fail, got {resp.status}"

        # Delete
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "DELETE",
            f"{backend_url}/api/roles/{role_name}",
        )
        assert resp.status == 200, f"Delete failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        role_name = None  # cleaned up
    finally:
        if role_name:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{role_name}")


# --- AC-4: Permission CRUD lifecycle ---


def test_permission_crud_lifecycle(admin_api_context: APIRequestContext, backend_url: str):
    """Full permission lifecycle: create -> update -> delete -> re-create (verify delete worked)."""
    perm_name = _unique_name("lifecycle-perm")
    updated_name = perm_name + "-updated"
    cleanup_name = perm_name

    try:
        # Create
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/permissions",
            data={"name": perm_name, "description": "E2E lifecycle test"},
        )
        assert resp.status == 201, f"Create failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        # Update (rename)
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "PUT",
            f"{backend_url}/api/permissions/{perm_name}",
            data={"new_name": updated_name, "description": "Updated description"},
        )
        assert resp.status == 200, f"Update failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        cleanup_name = updated_name

        # Verify old name no longer exists (re-create with old name should succeed)
        resp, _ = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/permissions",
            data={"name": perm_name, "description": "re-create after rename"},
        )
        assert resp.status == 201, f"Re-create old name should succeed after rename, got {resp.status}"
        # Clean up the re-created permission
        with contextlib.suppress(Exception):
            admin_api_context.delete(f"{backend_url}/api/permissions/{perm_name}")

        # Delete the renamed permission
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "DELETE",
            f"{backend_url}/api/permissions/{updated_name}",
        )
        assert resp.status == 200, f"Delete failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        cleanup_name = None

        # Verify deletion by re-creating with same name (should succeed if deleted)
        resp, _ = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/permissions",
            data={"name": updated_name, "description": "re-create after delete"},
        )
        assert resp.status == 201, f"Re-create after delete should succeed, got {resp.status}"
        cleanup_name = updated_name
    finally:
        if cleanup_name:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/permissions/{cleanup_name}")


# --- AC-5: Role CRUD with permission mapping ---


def test_role_crud_lifecycle(admin_api_context: APIRequestContext, backend_url: str):
    """Full role lifecycle: create -> update -> delete -> re-create (verify delete worked).

    Creates test permissions first since TF-seeded permissions may not exist in the
    Descope project used for CI.
    """
    role_name = _unique_name("lifecycle-role")
    updated_name = role_name + "-updated"
    cleanup_name = role_name
    perm_a = _unique_name("perm-a")
    perm_b = _unique_name("perm-b")
    perm_c = _unique_name("perm-c")
    created_perms = []

    try:
        # Create test permissions first
        for perm in [perm_a, perm_b, perm_c]:
            resp, _ = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/permissions",
                data={"name": perm, "description": "E2E test perm for role lifecycle"},
            )
            assert resp.status == 201, f"Create permission '{perm}' failed: {resp.status}"
            created_perms.append(perm)

        # Create role with permissions
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles",
            data={
                "name": role_name,
                "description": "E2E lifecycle test",
                "permission_names": [perm_a, perm_b],
            },
        )
        assert resp.status == 201, f"Create role failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        # Update (rename + change permissions)
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "PUT",
            f"{backend_url}/api/roles/{role_name}",
            data={
                "new_name": updated_name,
                "description": "Updated lifecycle test",
                "permission_names": [perm_a, perm_c],
            },
        )
        assert resp.status == 200, f"Update failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        cleanup_name = updated_name

        # Delete
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "DELETE",
            f"{backend_url}/api/roles/{updated_name}",
        )
        assert resp.status == 200, f"Delete failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
        cleanup_name = None

        # Verify deletion by re-creating with same name
        resp, _ = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles",
            data={"name": updated_name, "description": "re-create after delete"},
        )
        assert resp.status == 201, f"Re-create after delete should succeed, got {resp.status}"
        cleanup_name = updated_name
    finally:
        if cleanup_name:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{cleanup_name}")
        for perm in created_perms:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/permissions/{perm}")


# --- AC-6: Runtime role works with /roles/assign ---


def test_runtime_role_assignment(admin_api_context: APIRequestContext, backend_url: str, test_tenant_id: str):
    """Runtime-created role can be assigned to a user via /roles/assign.

    Creates a fresh test user to avoid issues with Descope project's read
    operations returning 500 (can't verify existing user tenant associations).
    """
    import httpx as _httpx

    role_name = _unique_name("assign-role")
    test_email = f"e2e-assign-{uuid.uuid4().hex[:8]}@test.example.com"
    cleanup_role = False
    cleanup_user = False

    _project_id = os.environ.get("DESCOPE_PROJECT_ID", "")
    _mgmt_key = os.environ.get("DESCOPE_MANAGEMENT_KEY", "")
    _mgmt_auth = {"Authorization": f"Bearer {_project_id}:{_mgmt_key}"}
    _base = os.environ.get("DESCOPE_BASE_URL", "https://api.descope.com")

    # Create a fresh test user with tenant association
    with _httpx.Client(timeout=30) as hc:
        create_resp = hc.post(
            f"{_base}/v1/mgmt/user/create",
            headers=_mgmt_auth,
            json={
                "loginId": test_email,
                "email": test_email,
                "name": "E2E Assign Test",
                "tenants": [{"tenantId": test_tenant_id}],
                "verifiedEmail": True,
                "test": True,
            },
        )
        print(f"[E2E] Create test user: {create_resp.status_code} {create_resp.text[:200]}")
        assert create_resp.status_code == 200, f"Create user failed: {create_resp.status_code}"
        cleanup_user = True

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

        # Assign the runtime role to the test user
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles/assign",
            data={"user_id": test_email, "tenant_id": test_tenant_id, "role_names": [role_name]},
        )
        assert resp.status == 200, f"Assign failed: {resp.status} — {resp.text()}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        # Remove the assigned role (cleanup user state)
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles/remove",
            data={"user_id": test_email, "tenant_id": test_tenant_id, "role_names": [role_name]},
        )
        assert resp.status == 200, f"Remove failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS
    finally:
        if cleanup_role:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{role_name}")
        if cleanup_user:
            with contextlib.suppress(Exception), _httpx.Client(timeout=10) as hc:
                hc.post(
                    f"{_base}/v1/mgmt/user/delete",
                    headers=_mgmt_auth,
                    json={"loginId": test_email},
                )


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
