"""Unit tests for TenantRepository — data access layer tests against real Postgres.

Tests cover:
- create: happy path, duplicate name → RepositoryConflictError
- get: found, not found
- get_by_name: found, not found
- list_all: returns all tenants
- update: happy path
- get_users_with_roles: 3-way JOIN returning (User, Role) tuples
"""

import uuid

import pytest

from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import User, UserStatus
from app.repositories.tenant import TenantRepository
from app.repositories.user import RepositoryConflictError

pytestmark = pytest.mark.asyncio


def _make_tenant(**overrides) -> Tenant:
    defaults = {"id": uuid.uuid4(), "name": f"tenant-{uuid.uuid4().hex[:8]}"}
    defaults.update(overrides)
    return Tenant(**defaults)


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


def _make_role(tenant_id=None, **overrides) -> Role:
    defaults = {
        "id": uuid.uuid4(),
        "name": f"role-{uuid.uuid4().hex[:8]}",
        "tenant_id": tenant_id,
    }
    defaults.update(overrides)
    return Role(**defaults)


async def test_create_tenant(db_session):
    repo = TenantRepository(db_session)
    tenant = _make_tenant()
    created = await repo.create(tenant)
    assert created.id == tenant.id
    assert created.name == tenant.name
    assert created.created_at is not None


async def test_create_tenant_duplicate_name(db_session):
    repo = TenantRepository(db_session)
    name = f"dup-tenant-{uuid.uuid4().hex[:8]}"
    await repo.create(_make_tenant(name=name))
    with pytest.raises(RepositoryConflictError):
        await repo.create(_make_tenant(name=name))


async def test_get_tenant(db_session):
    repo = TenantRepository(db_session)
    tenant = _make_tenant()
    await repo.create(tenant)
    fetched = await repo.get(tenant.id)
    assert fetched is not None
    assert fetched.id == tenant.id
    assert fetched.name == tenant.name


async def test_get_tenant_not_found(db_session):
    repo = TenantRepository(db_session)
    assert await repo.get(uuid.uuid4()) is None


async def test_get_by_name(db_session):
    repo = TenantRepository(db_session)
    tenant = _make_tenant()
    await repo.create(tenant)
    fetched = await repo.get_by_name(tenant.name)
    assert fetched is not None
    assert fetched.id == tenant.id


async def test_get_by_name_not_found(db_session):
    repo = TenantRepository(db_session)
    assert await repo.get_by_name("nonexistent-tenant") is None


async def test_list_all(db_session):
    repo = TenantRepository(db_session)
    t1 = _make_tenant()
    t2 = _make_tenant()
    await repo.create(t1)
    await repo.create(t2)

    all_tenants = await repo.list_all()
    tenant_ids = [t.id for t in all_tenants]
    assert t1.id in tenant_ids
    assert t2.id in tenant_ids


async def test_update_tenant(db_session):
    repo = TenantRepository(db_session)
    tenant = _make_tenant()
    await repo.create(tenant)
    tenant.name = f"updated-{uuid.uuid4().hex[:8]}"
    updated = await repo.update(tenant)
    assert updated.name == tenant.name


async def test_get_users_with_roles(db_session):
    """Verify 3-way JOIN returns (User, Role) tuples for a tenant."""
    repo = TenantRepository(db_session)

    tenant = _make_tenant()
    await repo.create(tenant)

    user = _make_user()
    db_session.add(user)
    role = _make_role(tenant_id=tenant.id)
    db_session.add(role)
    await db_session.flush()

    assignment = UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id)
    db_session.add(assignment)
    await db_session.flush()

    results = await repo.get_users_with_roles(tenant.id)
    assert len(results) == 1
    result_user, result_role = results[0]
    assert result_user.id == user.id
    assert result_role.id == role.id


async def test_get_users_with_roles_empty(db_session):
    repo = TenantRepository(db_session)
    tenant = _make_tenant()
    await repo.create(tenant)
    results = await repo.get_users_with_roles(tenant.id)
    assert results == []
