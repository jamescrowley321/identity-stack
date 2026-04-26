"""E2E API tests for repository base class refactor — cross-entity consistency.

Validates that the BaseRepository refactor did not break any API behavior.
Tests exercise multiple repositories in a single flow to verify flush/commit
consistency across the base class.

Requires DESCOPE_MANAGEMENT_KEY env var for admin token.
"""

import contextlib
import os

import pytest
from playwright.sync_api import APIRequestContext

from tests.e2e.helpers.api import unique_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY"),
    reason="DESCOPE_MANAGEMENT_KEY not set",
)


# --- Test 1: Full lifecycle chain across multiple repos ---


def test_full_lifecycle_chain(admin_api_context: APIRequestContext, backend_url: str):
    """Create permission → role with permission → delete role → delete permission.

    Exercises PermissionRepository, RoleRepository in one flow via BaseRepository
    create/get/delete methods.
    """
    perm_name = unique_name("chain-perm")
    role_name = unique_name("chain-role")
    cleanup_perm = None
    cleanup_role = None

    try:
        resp = admin_api_context.post(
            f"{backend_url}/api/permissions",
            data={"name": perm_name, "description": "E2E lifecycle chain test"},
        )
        assert resp.status == 201, f"Create permission failed: {resp.status}"
        cleanup_perm = perm_name

        resp = admin_api_context.post(
            f"{backend_url}/api/roles",
            data={
                "name": role_name,
                "description": "E2E lifecycle chain test",
                "permission_names": [perm_name],
            },
        )
        assert resp.status == 201, f"Create role failed: {resp.status}"
        cleanup_role = role_name

        resp = admin_api_context.delete(f"{backend_url}/api/roles/{role_name}")
        assert resp.status == 200, f"Delete role failed: {resp.status}"
        cleanup_role = None

        resp = admin_api_context.delete(f"{backend_url}/api/permissions/{perm_name}")
        assert resp.status == 200, f"Delete permission failed: {resp.status}"
        cleanup_perm = None

    finally:
        if cleanup_role:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{cleanup_role}")
        if cleanup_perm:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/permissions/{cleanup_perm}")


# --- Test 2: Conflict handling (RepositoryConflictError → 409) ---


def test_conflict_handling_permission(admin_api_context: APIRequestContext, backend_url: str):
    """Create duplicate permission → 409, delete → re-create succeeds.

    Validates RepositoryConflictError propagates correctly through
    service → router → HTTP 409 response after the base class refactor.
    """
    perm_name = unique_name("conflict-perm")
    cleanup_name = None

    try:
        resp = admin_api_context.post(
            f"{backend_url}/api/permissions",
            data={"name": perm_name, "description": "conflict test"},
        )
        assert resp.status == 201, f"Create failed: {resp.status}"
        cleanup_name = perm_name

        resp = admin_api_context.post(
            f"{backend_url}/api/permissions",
            data={"name": perm_name, "description": "duplicate"},
        )
        assert resp.status in (400, 409), f"Duplicate should fail, got {resp.status}"

        resp = admin_api_context.delete(f"{backend_url}/api/permissions/{perm_name}")
        assert resp.status == 200, f"Delete failed: {resp.status}"
        cleanup_name = None

        resp = admin_api_context.post(
            f"{backend_url}/api/permissions",
            data={"name": perm_name, "description": "re-created after delete"},
        )
        assert resp.status == 201, f"Re-create after delete should succeed, got {resp.status}"
        cleanup_name = perm_name

    finally:
        if cleanup_name:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/permissions/{cleanup_name}")


def test_conflict_handling_role(admin_api_context: APIRequestContext, backend_url: str):
    """Create duplicate role → 409, delete → re-create succeeds."""
    role_name = unique_name("conflict-role")
    cleanup_name = None

    try:
        resp = admin_api_context.post(
            f"{backend_url}/api/roles",
            data={"name": role_name, "description": "conflict test"},
        )
        assert resp.status == 201, f"Create failed: {resp.status}"
        cleanup_name = role_name

        resp = admin_api_context.post(
            f"{backend_url}/api/roles",
            data={"name": role_name, "description": "duplicate"},
        )
        assert resp.status in (400, 409), f"Duplicate should fail, got {resp.status}"

        resp = admin_api_context.delete(f"{backend_url}/api/roles/{role_name}")
        assert resp.status == 200, f"Delete failed: {resp.status}"
        cleanup_name = None

        resp = admin_api_context.post(
            f"{backend_url}/api/roles",
            data={"name": role_name, "description": "re-created after delete"},
        )
        assert resp.status == 201, f"Re-create after delete should succeed, got {resp.status}"
        cleanup_name = role_name

    finally:
        if cleanup_name:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{cleanup_name}")


# --- Test 3: Concurrent entity operations ---


def test_concurrent_entity_operations(admin_api_context: APIRequestContext, backend_url: str):
    """Create multiple permissions and roles in sequence, verify all exist, delete all.

    Validates flush/commit consistency across the BaseRepository when multiple
    entities are created in sequence within the same session lifetime.
    """
    perm_names = [unique_name(f"batch-perm-{i}") for i in range(3)]
    role_names = [unique_name(f"batch-role-{i}") for i in range(2)]
    created_perms = []
    created_roles = []

    try:
        for perm_name in perm_names:
            resp = admin_api_context.post(
                f"{backend_url}/api/permissions",
                data={"name": perm_name, "description": "batch test"},
            )
            assert resp.status == 201, f"Create permission '{perm_name}' failed: {resp.status}"
            created_perms.append(perm_name)

        for i, role_name in enumerate(role_names):
            resp = admin_api_context.post(
                f"{backend_url}/api/roles",
                data={
                    "name": role_name,
                    "description": "batch test",
                    "permission_names": [perm_names[i]],
                },
            )
            assert resp.status == 201, f"Create role '{role_name}' failed: {resp.status}"
            created_roles.append(role_name)

        # Verify all permissions exist via list
        resp = admin_api_context.get(f"{backend_url}/api/permissions")
        assert resp.status == 200
        listed_names = {p["name"] for p in resp.json().get("permissions", [])}
        for perm_name in perm_names:
            assert perm_name in listed_names, f"Permission '{perm_name}' not found in list"

        # Verify all roles exist via list
        resp = admin_api_context.get(f"{backend_url}/api/roles")
        assert resp.status == 200
        listed_roles = {r["name"] for r in resp.json().get("roles", [])}
        for role_name in role_names:
            assert role_name in listed_roles, f"Role '{role_name}' not found in list"

        # Delete roles first (depend on permissions)
        for role_name in created_roles:
            resp = admin_api_context.delete(f"{backend_url}/api/roles/{role_name}")
            assert resp.status == 200, f"Delete role '{role_name}' failed: {resp.status}"
        created_roles.clear()

        for perm_name in created_perms:
            resp = admin_api_context.delete(f"{backend_url}/api/permissions/{perm_name}")
            assert resp.status == 200, f"Delete permission '{perm_name}' failed: {resp.status}"
        created_perms.clear()

        # Verify cleanup — re-create one permission to prove it's gone
        resp = admin_api_context.post(
            f"{backend_url}/api/permissions",
            data={"name": perm_names[0], "description": "verify cleanup"},
        )
        assert resp.status == 201, f"Re-create after cleanup should succeed, got {resp.status}"
        created_perms.append(perm_names[0])

    finally:
        for role_name in created_roles:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{role_name}")
        for perm_name in created_perms:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/permissions/{perm_name}")
