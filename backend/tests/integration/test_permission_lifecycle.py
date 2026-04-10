"""Integration lifecycle tests for PermissionService against real Postgres.

AC-2.4.5: Full CRUD lifecycle — create → get → update → get → delete → verify gone.
Uses PermissionService + PermissionRepository + NoOpSyncAdapter + real Postgres.
"""

import uuid

import pytest

from app.repositories.permission import PermissionRepository
from app.services.adapters.noop import NoOpSyncAdapter
from app.services.permission import PermissionService


@pytest.fixture
def permission_service(db_session):
    repo = PermissionRepository(db_session)
    adapter = NoOpSyncAdapter()
    return PermissionService(repository=repo, adapter=adapter)


@pytest.mark.asyncio
async def test_permission_create_get_update_delete(db_session, permission_service):
    """Full permission lifecycle: create → get → update → get → delete → verify gone."""
    suffix = uuid.uuid4().hex[:8]

    # --- Create ---
    result = await permission_service.create_permission(
        name=f"perm.lifecycle.{suffix}",
        description="initial description",
    )
    assert result.is_ok()
    perm_data = result.ok
    perm_id = perm_data["id"]
    assert perm_data["name"] == f"perm.lifecycle.{suffix}"
    assert perm_data["description"] == "initial description"

    # --- Get ---
    result = await permission_service.get_permission(permission_id=perm_id)
    assert result.is_ok()
    assert result.ok["name"] == f"perm.lifecycle.{suffix}"

    # --- Update ---
    update_suffix = uuid.uuid4().hex[:8]
    result = await permission_service.update_permission(
        permission_id=perm_id,
        name=f"perm.lifecycle.{update_suffix}",
        description="updated description",
    )
    assert result.is_ok()
    assert result.ok["name"] == f"perm.lifecycle.{update_suffix}"
    assert result.ok["description"] == "updated description"

    # --- Get after update ---
    result = await permission_service.get_permission(permission_id=perm_id)
    assert result.is_ok()
    assert result.ok["name"] == f"perm.lifecycle.{update_suffix}"
    assert result.ok["description"] == "updated description"

    # --- Delete ---
    result = await permission_service.delete_permission(permission_id=perm_id)
    assert result.is_ok()
    assert result.ok["status"] == "deleted"

    # --- Verify gone ---
    result = await permission_service.get_permission(permission_id=perm_id)
    assert result.is_error()
    assert "not found" in result.error.message


@pytest.mark.asyncio
async def test_permission_duplicate_name(db_session, permission_service):
    """Creating a permission with a duplicate name returns Conflict."""
    suffix = uuid.uuid4().hex[:8]
    result = await permission_service.create_permission(name=f"perm.dup.{suffix}")
    assert result.is_ok()

    result = await permission_service.create_permission(name=f"perm.dup.{suffix}")
    assert result.is_error()
    assert "already exists" in result.error.message


@pytest.mark.asyncio
async def test_permission_get_not_found(db_session, permission_service):
    """Getting a nonexistent permission returns NotFound."""
    result = await permission_service.get_permission(permission_id=uuid.uuid4())
    assert result.is_error()
    assert "not found" in result.error.message


@pytest.mark.asyncio
async def test_permission_list(db_session, permission_service):
    """List permissions returns all created permissions."""
    suffix = uuid.uuid4().hex[:8]
    result_a = await permission_service.create_permission(name=f"perm.list.a.{suffix}")
    assert result_a.is_ok()
    result_b = await permission_service.create_permission(name=f"perm.list.b.{suffix}")
    assert result_b.is_ok()

    result = await permission_service.list_permissions()
    assert result.is_ok()
    names = [p["name"] for p in result.ok]
    assert f"perm.list.a.{suffix}" in names
    assert f"perm.list.b.{suffix}" in names
