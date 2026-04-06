"""Integration lifecycle tests for UserService against real Postgres.

AC-2.4.5: Full CRUD lifecycle — create → get → update → get → deactivate → get.
Uses UserService + UserRepository + NoOpSyncAdapter + real Postgres via testcontainers.
"""

import uuid

import pytest
import pytest_asyncio

from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.user import UserRepository
from app.services.adapters.noop import NoOpSyncAdapter
from app.services.user import UserService


@pytest.fixture
def user_service(db_session):
    repo = UserRepository(db_session)
    assignment_repo = UserTenantRoleRepository(db_session)
    adapter = NoOpSyncAdapter()
    return UserService(repository=repo, adapter=adapter, assignment_repository=assignment_repo)


@pytest_asyncio.fixture(loop_scope="session")
async def seed_tenant(db_session):
    """Create a tenant for user lifecycle tests."""
    suffix = uuid.uuid4().hex[:8]
    tenant = Tenant(name=f"lifecycle-tenant-{suffix}", domains=[f"{suffix}.test"])
    db_session.add(tenant)
    await db_session.flush()
    return tenant


@pytest_asyncio.fixture(loop_scope="session")
async def seed_role(db_session, seed_tenant):
    """Create a role scoped to the seed tenant."""
    suffix = uuid.uuid4().hex[:8]
    role = Role(name=f"lifecycle-role-{suffix}", description="test role", tenant_id=seed_tenant.id)
    db_session.add(role)
    await db_session.flush()
    return role


@pytest.mark.asyncio
async def test_user_create_get_update_deactivate(db_session, user_service, seed_tenant, seed_role):
    """Full user lifecycle: create → get → update → get → deactivate → get."""
    tenant_id = seed_tenant.id
    suffix = uuid.uuid4().hex[:8]

    # --- Create ---
    result = await user_service.create_user(
        tenant_id=tenant_id,
        email=f"lifecycle-{suffix}@test.com",
        user_name=f"lifecycle-user-{suffix}",
        given_name="Life",
        family_name="Cycle",
    )
    assert result.is_ok()
    user_data = result.ok
    user_id = user_data["id"]
    assert user_data["email"] == f"lifecycle-{suffix}@test.com"
    assert user_data["user_name"] == f"lifecycle-user-{suffix}"
    assert user_data["given_name"] == "Life"
    assert user_data["family_name"] == "Cycle"
    assert user_data["status"] == "active"

    # --- Get ---
    result = await user_service.get_user(tenant_id=tenant_id, user_id=user_id)
    assert result.is_ok()
    assert result.ok["email"] == f"lifecycle-{suffix}@test.com"

    # --- Update ---
    update_suffix = uuid.uuid4().hex[:8]
    result = await user_service.update_user(
        tenant_id=tenant_id,
        user_id=user_id,
        email=f"updated-{update_suffix}@test.com",
        given_name="Updated",
    )
    assert result.is_ok()
    updated = result.ok
    assert updated["email"] == f"updated-{update_suffix}@test.com"
    assert updated["given_name"] == "Updated"
    assert updated["family_name"] == "Cycle"

    # --- Get after update ---
    result = await user_service.get_user(tenant_id=tenant_id, user_id=user_id)
    assert result.is_ok()
    assert result.ok["email"] == f"updated-{update_suffix}@test.com"
    assert result.ok["given_name"] == "Updated"

    # --- Assign role (required for deactivate's tenant membership check) ---
    assignment = UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=seed_role.id)
    db_session.add(assignment)
    await db_session.commit()

    # --- Deactivate ---
    result = await user_service.deactivate_user(tenant_id=tenant_id, user_id=user_id)
    assert result.is_ok()
    assert result.ok["status"] == "inactive"

    # --- Get after deactivate ---
    result = await user_service.get_user(tenant_id=tenant_id, user_id=user_id)
    assert result.is_ok()
    assert result.ok["status"] == "inactive"


@pytest.mark.asyncio
async def test_user_create_duplicate_email(db_session, user_service, seed_tenant):
    """Creating a user with a duplicate email returns Conflict."""
    tenant_id = seed_tenant.id
    suffix = uuid.uuid4().hex[:8]

    result = await user_service.create_user(
        tenant_id=tenant_id,
        email=f"duplicate-{suffix}@test.com",
        user_name=f"first-user-{suffix}",
    )
    assert result.is_ok()

    result = await user_service.create_user(
        tenant_id=tenant_id,
        email=f"duplicate-{suffix}@test.com",
        user_name=f"second-user-{suffix}",
    )
    assert result.is_error()
    assert "already exists" in result.error.message


@pytest.mark.asyncio
async def test_user_get_not_found(db_session, user_service, seed_tenant):
    """Getting a nonexistent user returns NotFound."""
    result = await user_service.get_user(tenant_id=seed_tenant.id, user_id=uuid.uuid4())
    assert result.is_error()
    assert "not found" in result.error.message


@pytest.mark.asyncio
async def test_user_search_tenant_scoped(db_session, user_service, seed_tenant, seed_role):
    """Search returns only users with role assignments in the tenant."""
    tenant_id = seed_tenant.id
    suffix = uuid.uuid4().hex[:8]

    # Create a user but don't assign a role — should not appear in search
    result = await user_service.create_user(
        tenant_id=tenant_id,
        email=f"no-role-{suffix}@test.com",
        user_name=f"no-role-user-{suffix}",
    )
    assert result.is_ok()

    # Create another user and assign a role — should appear
    result = await user_service.create_user(
        tenant_id=tenant_id,
        email=f"has-role-{suffix}@test.com",
        user_name=f"has-role-user-{suffix}",
    )
    assert result.is_ok()
    user_with_role_id = result.ok["id"]

    assignment = UserTenantRole(user_id=user_with_role_id, tenant_id=tenant_id, role_id=seed_role.id)
    db_session.add(assignment)
    await db_session.commit()

    search_result = await user_service.search_users(tenant_id=tenant_id)
    assert search_result.is_ok()
    emails = [u["email"] for u in search_result.ok]
    assert f"has-role-{suffix}@test.com" in emails
    assert f"no-role-{suffix}@test.com" not in emails
