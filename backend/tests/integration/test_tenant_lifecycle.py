"""Integration lifecycle tests for TenantService against real Postgres.

AC-2.4.5: Full CRUD lifecycle — create → get, plus get_tenant_users_with_roles.
Uses TenantService + TenantRepository + NoOpSyncAdapter + real Postgres.
"""

import uuid

import pytest

from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.user import User
from app.repositories.tenant import TenantRepository
from app.services.adapters.noop import NoOpSyncAdapter
from app.services.tenant import TenantService


@pytest.fixture
def tenant_service(db_session):
    repo = TenantRepository(db_session)
    adapter = NoOpSyncAdapter()
    return TenantService(repository=repo, adapter=adapter)


@pytest.mark.asyncio
async def test_tenant_create_and_get(db_session, tenant_service):
    """Create a tenant and retrieve it by ID."""
    # --- Create ---
    result = await tenant_service.create_tenant(
        name="lifecycle-tenant",
        domains=["lifecycle.example.com"],
    )
    assert result.is_ok()
    tenant_data = result.ok
    tenant_id = tenant_data["id"]
    assert tenant_data["name"] == "lifecycle-tenant"
    assert tenant_data["domains"] == ["lifecycle.example.com"]
    assert tenant_data["status"] == "active"

    # --- Get ---
    result = await tenant_service.get_tenant(tenant_id=tenant_id)
    assert result.is_ok()
    assert result.ok["name"] == "lifecycle-tenant"
    assert result.ok["domains"] == ["lifecycle.example.com"]


@pytest.mark.asyncio
async def test_tenant_duplicate_name(db_session, tenant_service):
    """Creating a tenant with a duplicate name returns Conflict."""
    result = await tenant_service.create_tenant(name="dup-tenant")
    assert result.is_ok()

    result = await tenant_service.create_tenant(name="dup-tenant")
    assert result.is_error()
    assert "already exists" in result.error.message


@pytest.mark.asyncio
async def test_tenant_get_not_found(db_session, tenant_service):
    """Getting a nonexistent tenant returns NotFound."""
    result = await tenant_service.get_tenant(tenant_id=uuid.uuid4())
    assert result.is_error()
    assert "not found" in result.error.message


@pytest.mark.asyncio
async def test_tenant_users_with_roles_empty(db_session, tenant_service):
    """get_tenant_users_with_roles returns empty list for tenant with no assignments."""
    create_result = await tenant_service.create_tenant(name="empty-tenant")
    assert create_result.is_ok()
    tenant_id = create_result.ok["id"]

    result = await tenant_service.get_tenant_users_with_roles(tenant_id=tenant_id)
    assert result.is_ok()
    assert result.ok == []


@pytest.mark.asyncio
async def test_tenant_users_with_roles_populated(db_session, tenant_service):
    """get_tenant_users_with_roles returns users with their assigned roles."""
    # Create tenant via service
    create_result = await tenant_service.create_tenant(name="populated-tenant")
    assert create_result.is_ok()
    tenant_id = create_result.ok["id"]

    # Seed user and role directly via ORM (they're prerequisites, not under test)
    user = User(email="tenant-user@test.com", user_name="tenant-user")
    db_session.add(user)
    await db_session.flush()

    role = Role(name="tenant-role", description="test", tenant_id=tenant_id)
    db_session.add(role)
    await db_session.flush()

    assignment = UserTenantRole(user_id=user.id, tenant_id=tenant_id, role_id=role.id)
    db_session.add(assignment)
    await db_session.flush()
    await db_session.commit()

    # Query via service
    result = await tenant_service.get_tenant_users_with_roles(tenant_id=tenant_id)
    assert result.is_ok()
    users = result.ok
    assert len(users) == 1
    assert users[0]["email"] == "tenant-user@test.com"
    assert len(users[0]["roles"]) == 1
    assert users[0]["roles"][0]["name"] == "tenant-role"


@pytest.mark.asyncio
async def test_tenant_users_with_roles_multiple_roles(db_session, tenant_service):
    """A user with multiple roles appears once with all roles listed."""
    create_result = await tenant_service.create_tenant(name="multi-role-tenant")
    assert create_result.is_ok()
    tenant_id = create_result.ok["id"]

    user = User(email="multi-role@test.com", user_name="multi-role-user")
    db_session.add(user)
    await db_session.flush()

    role1 = Role(name="role-alpha", description="alpha", tenant_id=tenant_id)
    role2 = Role(name="role-beta", description="beta", tenant_id=tenant_id)
    db_session.add(role1)
    db_session.add(role2)
    await db_session.flush()

    for role in [role1, role2]:
        assignment = UserTenantRole(user_id=user.id, tenant_id=tenant_id, role_id=role.id)
        db_session.add(assignment)
    await db_session.flush()
    await db_session.commit()

    result = await tenant_service.get_tenant_users_with_roles(tenant_id=tenant_id)
    assert result.is_ok()
    users = result.ok
    assert len(users) == 1
    role_names = {r["name"] for r in users[0]["roles"]}
    assert role_names == {"role-alpha", "role-beta"}
