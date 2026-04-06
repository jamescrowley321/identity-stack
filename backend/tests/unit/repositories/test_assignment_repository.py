"""Unit tests for UserTenantRoleRepository — data access layer tests against real Postgres.

Tests cover:
- create: happy path, duplicate assignment → RepositoryConflictError
- get: composite PK lookup (user_id, tenant_id, role_id)
- list_by_user_tenant: returns all role assignments for a user in a tenant
- delete: specific assignment (composite PK)
- delete_by_user_tenant: bulk delete all assignments for user in tenant
- Tenant isolation: data for tenant A not visible from tenant B queries
"""

import uuid

import pytest

from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.user import RepositoryConflictError

pytestmark = pytest.mark.asyncio


def _make_user(**overrides) -> User:
    defaults = {
        "id": uuid.uuid4(),
        "email": f"user-{uuid.uuid4().hex[:8]}@test.com",
        "user_name": "testuser",
        "given_name": "Test",
        "family_name": "User",
        "status": UserStatus.active,
    }
    defaults.update(overrides)
    return User(**defaults)


def _make_tenant(**overrides) -> Tenant:
    defaults = {"id": uuid.uuid4(), "name": f"tenant-{uuid.uuid4().hex[:8]}"}
    defaults.update(overrides)
    return Tenant(**defaults)


def _make_role(tenant_id=None, **overrides) -> Role:
    defaults = {
        "id": uuid.uuid4(),
        "name": f"role-{uuid.uuid4().hex[:8]}",
        "tenant_id": tenant_id,
    }
    defaults.update(overrides)
    return Role(**defaults)


async def _setup_entities(db_session):
    """Create prerequisite user, tenant, and role for assignment tests."""
    user = _make_user()
    tenant = _make_tenant()
    db_session.add_all([user, tenant])
    await db_session.flush()
    role = _make_role(tenant_id=tenant.id)
    db_session.add(role)
    await db_session.flush()
    return user, tenant, role


async def test_create_assignment(db_session):
    repo = UserTenantRoleRepository(db_session)
    user, tenant, role = await _setup_entities(db_session)

    assignment = UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id)
    created = await repo.create(assignment)
    assert created.user_id == user.id
    assert created.tenant_id == tenant.id
    assert created.role_id == role.id
    assert created.assigned_at is not None


async def test_create_assignment_duplicate(db_session):
    repo = UserTenantRoleRepository(db_session)
    user, tenant, role = await _setup_entities(db_session)

    a1 = UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id)
    await repo.create(a1)
    a2 = UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id)
    with pytest.raises(RepositoryConflictError):
        await repo.create(a2)


async def test_get_assignment(db_session):
    repo = UserTenantRoleRepository(db_session)
    user, tenant, role = await _setup_entities(db_session)

    assignment = UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id)
    await repo.create(assignment)

    fetched = await repo.get(user.id, tenant.id, role.id)
    assert fetched is not None
    assert fetched.user_id == user.id
    assert fetched.tenant_id == tenant.id
    assert fetched.role_id == role.id


async def test_get_assignment_not_found(db_session):
    repo = UserTenantRoleRepository(db_session)
    result = await repo.get(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert result is None


async def test_list_by_user_tenant(db_session):
    repo = UserTenantRoleRepository(db_session)
    user = _make_user()
    tenant = _make_tenant()
    db_session.add_all([user, tenant])
    await db_session.flush()
    r1 = _make_role(tenant_id=tenant.id)
    r2 = _make_role(tenant_id=tenant.id)
    db_session.add_all([r1, r2])
    await db_session.flush()

    a1 = UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=r1.id)
    a2 = UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=r2.id)
    await repo.create(a1)
    await repo.create(a2)

    assignments = await repo.list_by_user_tenant(user.id, tenant.id)
    assert len(assignments) == 2
    role_ids = {a.role_id for a in assignments}
    assert r1.id in role_ids
    assert r2.id in role_ids


async def test_delete_assignment(db_session):
    repo = UserTenantRoleRepository(db_session)
    user, tenant, role = await _setup_entities(db_session)

    assignment = UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id)
    await repo.create(assignment)

    assert await repo.delete(user.id, tenant.id, role.id) is True
    assert await repo.get(user.id, tenant.id, role.id) is None


async def test_delete_assignment_not_found(db_session):
    repo = UserTenantRoleRepository(db_session)
    assert await repo.delete(uuid.uuid4(), uuid.uuid4(), uuid.uuid4()) is False


async def test_delete_by_user_tenant(db_session):
    repo = UserTenantRoleRepository(db_session)
    user = _make_user()
    tenant = _make_tenant()
    db_session.add_all([user, tenant])
    await db_session.flush()
    r1 = _make_role(tenant_id=tenant.id)
    r2 = _make_role(tenant_id=tenant.id)
    db_session.add_all([r1, r2])
    await db_session.flush()

    a1 = UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=r1.id)
    a2 = UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=r2.id)
    await repo.create(a1)
    await repo.create(a2)

    count = await repo.delete_by_user_tenant(user.id, tenant.id)
    assert count == 2
    assert await repo.list_by_user_tenant(user.id, tenant.id) == []


async def test_tenant_isolation(db_session):
    """Assignments in tenant A must not be visible when querying tenant B."""
    repo = UserTenantRoleRepository(db_session)
    user = _make_user()
    tenant_a = _make_tenant()
    tenant_b = _make_tenant()
    db_session.add_all([user, tenant_a, tenant_b])
    await db_session.flush()
    role_a = _make_role(tenant_id=tenant_a.id)
    role_b = _make_role(tenant_id=tenant_b.id)
    db_session.add_all([role_a, role_b])
    await db_session.flush()

    # Assign user in tenant A only
    assignment = UserTenantRole(user_id=user.id, tenant_id=tenant_a.id, role_id=role_a.id)
    await repo.create(assignment)

    # Tenant A has the assignment
    a_results = await repo.list_by_user_tenant(user.id, tenant_a.id)
    assert len(a_results) == 1

    # Tenant B has nothing
    b_results = await repo.list_by_user_tenant(user.id, tenant_b.id)
    assert len(b_results) == 0
