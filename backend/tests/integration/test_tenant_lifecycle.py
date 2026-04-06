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
    suffix = uuid.uuid4().hex[:8]

    # --- Create ---
    result = await tenant_service.create_tenant(
        name=f"lifecycle-tenant-{suffix}",
        domains=[f"{suffix}.example.com"],
    )
    assert result.is_ok()
    tenant_data = result.ok
    tenant_id = tenant_data["id"]
    assert tenant_data["name"] == f"lifecycle-tenant-{suffix}"
    assert tenant_data["domains"] == [f"{suffix}.example.com"]
    assert tenant_data["status"] == "active"

    # --- Get ---
    result = await tenant_service.get_tenant(tenant_id=tenant_id)
    assert result.is_ok()
    assert result.ok["name"] == f"lifecycle-tenant-{suffix}"
    assert result.ok["domains"] == [f"{suffix}.example.com"]


@pytest.mark.asyncio
async def test_tenant_duplicate_name(db_session, tenant_service):
    """Creating a tenant with a duplicate name returns Conflict."""
    suffix = uuid.uuid4().hex[:8]
    result = await tenant_service.create_tenant(name=f"dup-tenant-{suffix}")
    assert result.is_ok()

    result = await tenant_service.create_tenant(name=f"dup-tenant-{suffix}")
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
    suffix = uuid.uuid4().hex[:8]
    create_result = await tenant_service.create_tenant(name=f"empty-tenant-{suffix}")
    assert create_result.is_ok()
    tenant_id = create_result.ok["id"]

    result = await tenant_service.get_tenant_users_with_roles(tenant_id=tenant_id)
    assert result.is_ok()
    assert result.ok == []


@pytest.mark.asyncio
async def test_tenant_users_with_roles_populated(db_session, tenant_service):
    """get_tenant_users_with_roles returns users with their assigned roles."""
    suffix = uuid.uuid4().hex[:8]

    # Create tenant via service
    create_result = await tenant_service.create_tenant(name=f"populated-tenant-{suffix}")
    assert create_result.is_ok()
    tenant_id = create_result.ok["id"]

    # Seed user and role directly via ORM (they're prerequisites, not under test)
    user = User(email=f"tenant-user-{suffix}@test.com", user_name=f"tenant-user-{suffix}")
    db_session.add(user)
    await db_session.flush()

    role = Role(name=f"tenant-role-{suffix}", description="test", tenant_id=tenant_id)
    db_session.add(role)
    await db_session.flush()

    assignment = UserTenantRole(user_id=user.id, tenant_id=tenant_id, role_id=role.id)
    db_session.add(assignment)
    await db_session.commit()

    # Query via service
    result = await tenant_service.get_tenant_users_with_roles(tenant_id=tenant_id)
    assert result.is_ok()
    users = result.ok
    assert len(users) == 1
    assert users[0]["email"] == f"tenant-user-{suffix}@test.com"
    assert len(users[0]["roles"]) == 1
    assert users[0]["roles"][0]["name"] == f"tenant-role-{suffix}"


@pytest.mark.asyncio
async def test_tenant_users_with_roles_multiple_roles(db_session, tenant_service):
    """A user with multiple roles appears once with all roles listed."""
    suffix = uuid.uuid4().hex[:8]

    create_result = await tenant_service.create_tenant(name=f"multi-role-tenant-{suffix}")
    assert create_result.is_ok()
    tenant_id = create_result.ok["id"]

    user = User(email=f"multi-role-{suffix}@test.com", user_name=f"multi-role-user-{suffix}")
    db_session.add(user)
    await db_session.flush()

    role1 = Role(name=f"role-alpha-{suffix}", description="alpha", tenant_id=tenant_id)
    role2 = Role(name=f"role-beta-{suffix}", description="beta", tenant_id=tenant_id)
    db_session.add(role1)
    db_session.add(role2)
    await db_session.flush()

    for role in [role1, role2]:
        assignment = UserTenantRole(user_id=user.id, tenant_id=tenant_id, role_id=role.id)
        db_session.add(assignment)
    await db_session.commit()

    result = await tenant_service.get_tenant_users_with_roles(tenant_id=tenant_id)
    assert result.is_ok()
    users = result.ok
    assert len(users) == 1
    role_names = {r["name"] for r in users[0]["roles"]}
    assert role_names == {f"role-alpha-{suffix}", f"role-beta-{suffix}"}
