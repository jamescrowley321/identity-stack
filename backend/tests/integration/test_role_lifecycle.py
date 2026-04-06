"""Integration lifecycle tests for RoleService against real Postgres.

AC-2.4.5: Full CRUD lifecycle — create → get → update → get → delete → verify gone.
Uses RoleService + RoleRepository + NoOpSyncAdapter + real Postgres via testcontainers.
"""

import uuid

import pytest
import pytest_asyncio

from app.models.identity.tenant import Tenant
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.services.adapters.noop import NoOpSyncAdapter
from app.services.permission import PermissionService
from app.services.role import RoleService


@pytest.fixture
def role_service(db_session):
    role_repo = RoleRepository(db_session)
    perm_repo = PermissionRepository(db_session)
    assignment_repo = UserTenantRoleRepository(db_session)
    adapter = NoOpSyncAdapter()
    return RoleService(
        repository=role_repo,
        permission_repository=perm_repo,
        assignment_repository=assignment_repo,
        adapter=adapter,
    )


@pytest.fixture
def permission_service(db_session):
    perm_repo = PermissionRepository(db_session)
    adapter = NoOpSyncAdapter()
    return PermissionService(repository=perm_repo, adapter=adapter)


@pytest_asyncio.fixture(loop_scope="session")
async def seed_tenant(db_session):
    """Create a tenant for role lifecycle tests."""
    tenant = Tenant(name="role-lifecycle-tenant", domains=[])
    db_session.add(tenant)
    await db_session.flush()
    return tenant


@pytest.mark.asyncio
async def test_role_create_get_update_delete(db_session, role_service, seed_tenant):
    """Full role lifecycle: create → get → update → get → delete → verify gone."""
    tenant_id = seed_tenant.id

    # --- Create ---
    result = await role_service.create_role(
        name="lifecycle-role",
        description="initial description",
        tenant_id=tenant_id,
    )
    assert result.is_ok()
    role_data = result.ok
    role_id = role_data["id"]
    assert role_data["name"] == "lifecycle-role"
    assert role_data["description"] == "initial description"
    assert role_data["tenant_id"] == tenant_id

    # --- Get ---
    result = await role_service.get_role(role_id=role_id)
    assert result.is_ok()
    assert result.ok["name"] == "lifecycle-role"

    # --- Update ---
    result = await role_service.update_role(
        role_id=role_id,
        name="updated-role",
        description="updated description",
    )
    assert result.is_ok()
    assert result.ok["name"] == "updated-role"
    assert result.ok["description"] == "updated description"

    # --- Get after update ---
    result = await role_service.get_role(role_id=role_id)
    assert result.is_ok()
    assert result.ok["name"] == "updated-role"

    # --- Delete ---
    result = await role_service.delete_role(role_id=role_id)
    assert result.is_ok()
    assert result.ok["status"] == "deleted"

    # --- Verify gone ---
    result = await role_service.get_role(role_id=role_id)
    assert result.is_error()
    assert "not found" in result.error.message


@pytest.mark.asyncio
async def test_role_with_permission_mapping(db_session, role_service, permission_service, seed_tenant):
    """Create a role with permissions, then verify mapping lifecycle."""
    # Create permissions first
    perm1_result = await permission_service.create_permission(name="role-test.read", description="read")
    assert perm1_result.is_ok()

    perm2_result = await permission_service.create_permission(name="role-test.write", description="write")
    assert perm2_result.is_ok()

    # Create role with permission_names
    result = await role_service.create_role(
        name="permissioned-role",
        tenant_id=seed_tenant.id,
        permission_names=["role-test.read", "role-test.write"],
    )
    assert result.is_ok()
    role_id = result.ok["id"]

    # Map an additional permission via map_permission_to_role
    perm3_result = await permission_service.create_permission(name="role-test.delete", description="delete")
    assert perm3_result.is_ok()
    perm3_id = perm3_result.ok["id"]

    map_result = await role_service.map_permission_to_role(role_id=role_id, permission_id=perm3_id)
    assert map_result.is_ok()
    assert map_result.ok["role_id"] == str(role_id)
    assert map_result.ok["permission_id"] == str(perm3_id)


@pytest.mark.asyncio
async def test_role_duplicate_name_in_scope(db_session, role_service, seed_tenant):
    """Creating a role with a duplicate name in the same tenant scope returns Conflict."""
    tenant_id = seed_tenant.id

    result = await role_service.create_role(name="unique-role", tenant_id=tenant_id)
    assert result.is_ok()

    result = await role_service.create_role(name="unique-role", tenant_id=tenant_id)
    assert result.is_error()
    assert "already exists" in result.error.message


@pytest.mark.asyncio
async def test_role_get_not_found(db_session, role_service):
    """Getting a nonexistent role returns NotFound."""
    result = await role_service.get_role(role_id=uuid.uuid4())
    assert result.is_error()
    assert "not found" in result.error.message
