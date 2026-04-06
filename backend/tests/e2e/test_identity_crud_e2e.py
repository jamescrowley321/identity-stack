"""E2E tests for canonical identity CRUD: users, roles, permissions, tenants.

Validates 3-tier auth enforcement and canonical response shapes (id, created_at,
updated_at) for Postgres-backed identity resources introduced by the onion
architecture refactor (stories 2.1-2.4).

Auth tiers:
  - Tier 1: Unauthenticated (api_context) → 401
  - Tier 2: Authenticated non-admin (auth_api_context) → 403
  - Tier 3: Admin (admin_api_context) → success (200/201)
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
        print(f"[E2E] {method} {url} → {resp.status}")
    return resp, elapsed_ms


# =============================================================================
# 3-Tier Auth Enforcement
# =============================================================================


class TestMemberAuthEnforcement:
    """Tier 1 (401) and Tier 2 (403) auth tests for /api/members endpoints."""

    DUMMY_UUID = "00000000-0000-0000-0000-000000000000"

    def test_unauth_list_members(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.get(f"{backend_url}/api/members")
        assert resp.status == 401

    def test_unauth_invite_member(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.post(
            f"{backend_url}/api/members/invite",
            data={"email": "nobody@test.example.com"},
        )
        assert resp.status == 401

    def test_unauth_deactivate_member(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.post(f"{backend_url}/api/members/{self.DUMMY_UUID}/deactivate")
        assert resp.status == 401

    def test_unauth_activate_member(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.post(f"{backend_url}/api/members/{self.DUMMY_UUID}/activate")
        assert resp.status == 401

    def test_unauth_remove_member(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.delete(f"{backend_url}/api/members/{self.DUMMY_UUID}")
        assert resp.status == 401

    def test_nonadmin_list_members(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.get(f"{backend_url}/api/members")
        assert resp.status == 403

    def test_nonadmin_invite_member(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.post(
            f"{backend_url}/api/members/invite",
            data={"email": "nobody@test.example.com"},
        )
        assert resp.status == 403

    def test_nonadmin_deactivate_member(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.post(f"{backend_url}/api/members/{self.DUMMY_UUID}/deactivate")
        assert resp.status == 403

    def test_nonadmin_activate_member(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.post(f"{backend_url}/api/members/{self.DUMMY_UUID}/activate")
        assert resp.status == 403

    def test_nonadmin_remove_member(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.delete(f"{backend_url}/api/members/{self.DUMMY_UUID}")
        assert resp.status == 403


class TestRoleAuthEnforcement:
    """Tier 1 (401) and Tier 2 (403) auth tests for /api/roles CRUD endpoints."""

    def test_unauth_list_roles(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.get(f"{backend_url}/api/roles")
        assert resp.status == 401

    def test_unauth_create_role(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.post(
            f"{backend_url}/api/roles",
            data={"name": "test", "description": "test"},
        )
        assert resp.status == 401

    def test_unauth_update_role(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.put(
            f"{backend_url}/api/roles/nonexistent",
            data={"new_name": "test"},
        )
        assert resp.status == 401

    def test_unauth_delete_role(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.delete(f"{backend_url}/api/roles/nonexistent")
        assert resp.status == 401

    def test_nonadmin_list_roles(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.get(f"{backend_url}/api/roles")
        assert resp.status == 403

    def test_nonadmin_create_role(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.post(
            f"{backend_url}/api/roles",
            data={"name": "test", "description": "test"},
        )
        assert resp.status == 403

    def test_nonadmin_update_role(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.put(
            f"{backend_url}/api/roles/nonexistent",
            data={"new_name": "test"},
        )
        assert resp.status == 403

    def test_nonadmin_delete_role(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.delete(f"{backend_url}/api/roles/nonexistent")
        assert resp.status == 403


class TestPermissionAuthEnforcement:
    """Tier 1 (401) and Tier 2 (403) auth tests for /api/permissions endpoints."""

    def test_unauth_list_permissions(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.get(f"{backend_url}/api/permissions")
        assert resp.status == 401

    def test_unauth_create_permission(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.post(
            f"{backend_url}/api/permissions",
            data={"name": "test", "description": "test"},
        )
        assert resp.status == 401

    def test_unauth_update_permission(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.put(
            f"{backend_url}/api/permissions/nonexistent",
            data={"new_name": "test"},
        )
        assert resp.status == 401

    def test_unauth_delete_permission(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.delete(f"{backend_url}/api/permissions/nonexistent")
        assert resp.status == 401

    def test_nonadmin_list_permissions(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.get(f"{backend_url}/api/permissions")
        assert resp.status == 403

    def test_nonadmin_create_permission(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.post(
            f"{backend_url}/api/permissions",
            data={"name": "test", "description": "test"},
        )
        assert resp.status == 403

    def test_nonadmin_update_permission(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.put(
            f"{backend_url}/api/permissions/nonexistent",
            data={"new_name": "test"},
        )
        assert resp.status == 403

    def test_nonadmin_delete_permission(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.delete(f"{backend_url}/api/permissions/nonexistent")
        assert resp.status == 403


class TestTenantAuthEnforcement:
    """Tier 1 (401) and Tier 2 (403) auth tests for /api/tenants endpoints."""

    def test_unauth_list_tenants(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.get(f"{backend_url}/api/tenants")
        assert resp.status == 401

    def test_unauth_get_current_tenant(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.get(f"{backend_url}/api/tenants/current")
        assert resp.status == 401

    def test_unauth_create_tenant(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.post(
            f"{backend_url}/api/tenants",
            data={"name": "test-tenant"},
        )
        assert resp.status == 401

    def test_nonadmin_create_tenant(self, auth_api_context: APIRequestContext, backend_url: str):
        """POST /tenants requires project-level admin role."""
        resp = auth_api_context.post(
            f"{backend_url}/api/tenants",
            data={"name": "test-tenant"},
        )
        assert resp.status == 403

    def test_nonadmin_get_current_tenant(self, auth_api_context: APIRequestContext, backend_url: str):
        """GET /tenants/current requires dct claim — OIDC tokens lack it."""
        resp = auth_api_context.get(f"{backend_url}/api/tenants/current")
        assert resp.status == 403


# =============================================================================
# Tier 3: Admin CRUD Operations with Canonical Response Shapes
# =============================================================================


class TestPermissionCrudCanonical:
    """Admin permission CRUD verifying canonical fields (id, created_at, updated_at)."""

    def test_permission_create_returns_canonical_fields(self, admin_api_context: APIRequestContext, backend_url: str):
        """POST /permissions returns response with id (UUID), created_at, updated_at."""
        perm_name = _unique_name("canon-perm")
        try:
            resp, elapsed_ms = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/permissions",
                data={"name": perm_name, "description": "canonical field test"},
            )
            assert resp.status == 201, f"Create failed: {resp.status}"
            assert elapsed_ms < MAX_API_RESPONSE_MS

            body = resp.json()
            # Verify canonical fields exist
            assert "id" in body, f"Missing 'id' in response: {body}"
            assert "name" in body, f"Missing 'name' in response: {body}"
            assert "created_at" in body, f"Missing 'created_at' in response: {body}"
            assert "updated_at" in body, f"Missing 'updated_at' in response: {body}"
            # Verify id is a valid UUID
            uuid.UUID(str(body["id"]))
            assert body["name"] == perm_name
        finally:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/permissions/{perm_name}")

    def test_permission_list_returns_canonical_fields(self, admin_api_context: APIRequestContext, backend_url: str):
        """GET /permissions returns list with canonical fields per item."""
        perm_name = _unique_name("list-perm")
        try:
            # Create a permission so the list is non-empty
            resp, _ = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/permissions",
                data={"name": perm_name, "description": "list test"},
            )
            assert resp.status == 201

            resp, elapsed_ms = _timed_request(
                admin_api_context,
                "GET",
                f"{backend_url}/api/permissions",
            )
            assert resp.status == 200, f"List failed: {resp.status}"
            assert elapsed_ms < MAX_API_RESPONSE_MS

            body = resp.json()
            assert "permissions" in body
            permissions = body["permissions"]
            assert len(permissions) > 0

            # Find our created permission and verify canonical fields
            match = next((p for p in permissions if p["name"] == perm_name), None)
            assert match is not None, f"Created permission '{perm_name}' not in list"
            assert "id" in match
            assert "created_at" in match
            assert "updated_at" in match
            uuid.UUID(str(match["id"]))
        finally:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/permissions/{perm_name}")

    def test_permission_update_returns_canonical_fields(self, admin_api_context: APIRequestContext, backend_url: str):
        """PUT /permissions/{name} returns updated resource with canonical fields."""
        perm_name = _unique_name("upd-perm")
        new_name = perm_name + "-renamed"
        cleanup_name = perm_name
        try:
            resp, _ = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/permissions",
                data={"name": perm_name, "description": "update test"},
            )
            assert resp.status == 201

            resp, elapsed_ms = _timed_request(
                admin_api_context,
                "PUT",
                f"{backend_url}/api/permissions/{perm_name}",
                data={"new_name": new_name, "description": "updated"},
            )
            assert resp.status == 200, f"Update failed: {resp.status}"
            cleanup_name = new_name  # update before further assertions — rename already happened
            assert elapsed_ms < MAX_API_RESPONSE_MS

            body = resp.json()
            assert "id" in body
            assert "updated_at" in body
            assert body["name"] == new_name
        finally:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/permissions/{cleanup_name}")


class TestRoleCrudCanonical:
    """Admin role CRUD verifying canonical fields (id, created_at, updated_at)."""

    def test_role_create_returns_canonical_fields(self, admin_api_context: APIRequestContext, backend_url: str):
        """POST /roles returns response with id (UUID), created_at, updated_at."""
        role_name = _unique_name("canon-role")
        try:
            resp, elapsed_ms = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/roles",
                data={"name": role_name, "description": "canonical field test"},
            )
            assert resp.status == 201, f"Create failed: {resp.status}"
            assert elapsed_ms < MAX_API_RESPONSE_MS

            body = resp.json()
            assert "id" in body, f"Missing 'id' in response: {body}"
            assert "name" in body, f"Missing 'name' in response: {body}"
            assert "created_at" in body, f"Missing 'created_at' in response: {body}"
            assert "updated_at" in body, f"Missing 'updated_at' in response: {body}"
            uuid.UUID(str(body["id"]))
            assert body["name"] == role_name
        finally:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{role_name}")

    def test_role_list_returns_canonical_fields(self, admin_api_context: APIRequestContext, backend_url: str):
        """GET /roles returns list with canonical fields per item."""
        role_name = _unique_name("list-role")
        try:
            resp, _ = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/roles",
                data={"name": role_name, "description": "list test"},
            )
            assert resp.status == 201

            resp, elapsed_ms = _timed_request(
                admin_api_context,
                "GET",
                f"{backend_url}/api/roles",
            )
            assert resp.status == 200, f"List failed: {resp.status}"
            assert elapsed_ms < MAX_API_RESPONSE_MS

            body = resp.json()
            assert "roles" in body
            roles = body["roles"]
            assert len(roles) > 0

            match = next((r for r in roles if r["name"] == role_name), None)
            assert match is not None, f"Created role '{role_name}' not in list"
            assert "id" in match
            assert "created_at" in match
            assert "updated_at" in match
            uuid.UUID(str(match["id"]))
        finally:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{role_name}")

    def test_role_update_returns_canonical_fields(self, admin_api_context: APIRequestContext, backend_url: str):
        """PUT /roles/{name} returns updated resource with canonical fields."""
        role_name = _unique_name("upd-role")
        new_name = role_name + "-renamed"
        cleanup_name = role_name
        try:
            resp, _ = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/roles",
                data={"name": role_name, "description": "update test"},
            )
            assert resp.status == 201

            resp, elapsed_ms = _timed_request(
                admin_api_context,
                "PUT",
                f"{backend_url}/api/roles/{role_name}",
                data={"new_name": new_name, "description": "updated"},
            )
            assert resp.status == 200, f"Update failed: {resp.status}"
            cleanup_name = new_name  # update before further assertions — rename already happened
            assert elapsed_ms < MAX_API_RESPONSE_MS

            body = resp.json()
            assert "id" in body
            assert "updated_at" in body
            assert body["name"] == new_name
        finally:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{cleanup_name}")


class TestMemberCrud:
    """Admin member CRUD operations via /api/members endpoints."""

    def test_list_members(self, admin_api_context: APIRequestContext, backend_url: str):
        """GET /members returns list of tenant members."""
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "GET",
            f"{backend_url}/api/members",
        )
        assert resp.status == 200, f"List members failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        body = resp.json()
        assert "members" in body
        assert isinstance(body["members"], list)

    def test_invite_member_returns_canonical_fields(self, admin_api_context: APIRequestContext, backend_url: str):
        """POST /members/invite creates user with canonical fields in response."""
        test_email = f"e2e-invite-{uuid.uuid4().hex[:8]}@test.example.com"
        created_user_id = None
        try:
            resp, elapsed_ms = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/members/invite",
                data={"email": test_email, "role_names": ["member"]},
            )
            # Router returns 200 (no status_code=201 on decorator) or 207 (sync failed)
            assert resp.status in (200, 207), f"Invite failed: {resp.status}"
            assert elapsed_ms < MAX_API_RESPONSE_MS

            body = resp.json()
            if resp.status == 207:
                # 207 = Postgres OK, Descope sync failed → RFC 9457 Problem Detail
                # Extract user_id from inner detail if available, otherwise skip
                pytest.skip("Invite returned 207 (sync failed) — canonical fields not in Problem Detail body")
            assert "user" in body, f"Missing 'user' key in invite response: {body.keys()}"
            user = body["user"]
            assert "id" in user, f"Missing 'id' in user response: {user}"
            assert "created_at" in user, f"Missing 'created_at' in user response: {user}"
            created_user_id = str(user["id"])
        finally:
            if created_user_id:
                with contextlib.suppress(Exception):
                    admin_api_context.delete(f"{backend_url}/api/members/{created_user_id}")

    def test_member_deactivate_activate_lifecycle(self, admin_api_context: APIRequestContext, backend_url: str):
        """Invite → deactivate → activate → remove lifecycle."""
        test_email = f"e2e-lifecycle-{uuid.uuid4().hex[:8]}@test.example.com"
        created_user_id = None
        try:
            # Invite
            resp, _ = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/members/invite",
                data={"email": test_email, "role_names": ["member"]},
            )
            # Router returns 200 (no status_code=201 on decorator) or 207 (sync failed)
            assert resp.status in (200, 207), f"Invite failed: {resp.status}"
            body = resp.json()
            if resp.status != 207 and "user" in body and "id" in body["user"]:
                created_user_id = str(body["user"]["id"])

            if not created_user_id:
                pytest.skip("Could not extract user_id from invite response")

            # Deactivate
            resp, elapsed_ms = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/members/{created_user_id}/deactivate",
            )
            assert resp.status == 200, f"Deactivate failed: {resp.status}"
            assert elapsed_ms < MAX_API_RESPONSE_MS
            body = resp.json()
            assert body.get("status") == "deactivated"

            # Activate
            resp, elapsed_ms = _timed_request(
                admin_api_context,
                "POST",
                f"{backend_url}/api/members/{created_user_id}/activate",
            )
            assert resp.status == 200, f"Activate failed: {resp.status}"
            assert elapsed_ms < MAX_API_RESPONSE_MS
            body = resp.json()
            assert body.get("status") == "activated"

            # Remove
            resp, elapsed_ms = _timed_request(
                admin_api_context,
                "DELETE",
                f"{backend_url}/api/members/{created_user_id}",
            )
            assert resp.status == 200, f"Remove failed: {resp.status}"
            assert elapsed_ms < MAX_API_RESPONSE_MS
            body = resp.json()
            assert body.get("status") == "removed"
            created_user_id = None  # cleaned up
        finally:
            if created_user_id:
                with contextlib.suppress(Exception):
                    admin_api_context.delete(f"{backend_url}/api/members/{created_user_id}")


class TestTenantOperations:
    """Admin tenant operations via /api/tenants endpoints."""

    def test_list_tenants_returns_jwt_tenants(self, admin_api_context: APIRequestContext, backend_url: str):
        """GET /tenants returns tenants from JWT claims."""
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "GET",
            f"{backend_url}/api/tenants",
        )
        assert resp.status == 200, f"List tenants failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        body = resp.json()
        assert "tenants" in body
        tenants = body["tenants"]
        assert isinstance(tenants, list)
        assert len(tenants) > 0, "Admin token should have at least one tenant"

        # Each tenant should have id, roles, permissions
        for tenant in tenants:
            assert "id" in tenant
            assert "roles" in tenant
            assert "permissions" in tenant

    def test_get_current_tenant(self, admin_api_context: APIRequestContext, backend_url: str):
        """GET /tenants/current returns current tenant context from dct claim."""
        resp, elapsed_ms = _timed_request(
            admin_api_context,
            "GET",
            f"{backend_url}/api/tenants/current",
        )
        assert resp.status == 200, f"Get current tenant failed: {resp.status}"
        assert elapsed_ms < MAX_API_RESPONSE_MS

        body = resp.json()
        assert "tenant_id" in body
        assert "tenant" in body, f"Missing 'tenant' key in current tenant response: {body.keys()}"
        tenant = body["tenant"]
        assert "id" in tenant, f"Missing 'id' in tenant response: {tenant}"
        assert "created_at" in tenant, f"Missing 'created_at' in tenant response: {tenant}"
        assert "updated_at" in tenant, f"Missing 'updated_at' in tenant response: {tenant}"
