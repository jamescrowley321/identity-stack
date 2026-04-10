"""Unit tests for RoleRepository — data access layer tests against real Postgres.

Tests cover:
- create: happy path, duplicate name+tenant → RepositoryConflictError
- get: found, not found
- get_by_name: global scope (tenant_id=None), tenant scope
- list_by_tenant: global roles, tenant-scoped roles
- update: happy path
- delete: existing, non-existent
- add_permission / remove_permission / get_permissions: permission mapping
- Name uniqueness: per-tenant (same name, different tenants → OK)
"""

import uuid

import pytest

from app.models.identity.role import Permission, Role
from app.models.identity.tenant import Tenant
from app.repositories.role import RoleRepository
from app.repositories.user import RepositoryConflictError

pytestmark = pytest.mark.asyncio


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


def _make_permission(**overrides) -> Permission:
    defaults = {
        "id": uuid.uuid4(),
        "name": f"perm-{uuid.uuid4().hex[:8]}",
        "description": "test permission",
    }
    defaults.update(overrides)
    return Permission(**defaults)


async def test_create_role(db_session):
    repo = RoleRepository(db_session)
    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()

    role = _make_role(tenant_id=tenant.id)
    created = await repo.create(role)
    assert created.id == role.id
    assert created.name == role.name


async def test_create_role_duplicate_name_same_tenant(db_session):
    repo = RoleRepository(db_session)
    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()

    name = f"dup-role-{uuid.uuid4().hex[:8]}"
    await repo.create(_make_role(tenant_id=tenant.id, name=name))
    with pytest.raises(RepositoryConflictError):
        await repo.create(_make_role(tenant_id=tenant.id, name=name))


async def test_create_role_same_name_different_tenants(db_session):
    """Same role name in different tenants should succeed."""
    repo = RoleRepository(db_session)
    t1 = _make_tenant()
    t2 = _make_tenant()
    db_session.add_all([t1, t2])
    await db_session.flush()

    name = f"shared-role-{uuid.uuid4().hex[:8]}"
    r1 = await repo.create(_make_role(tenant_id=t1.id, name=name))
    r2 = await repo.create(_make_role(tenant_id=t2.id, name=name))
    assert r1.id != r2.id


async def test_get_role(db_session):
    repo = RoleRepository(db_session)
    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()

    role = _make_role(tenant_id=tenant.id)
    await repo.create(role)
    fetched = await repo.get(role.id)
    assert fetched is not None
    assert fetched.name == role.name


async def test_get_role_not_found(db_session):
    repo = RoleRepository(db_session)
    assert await repo.get(uuid.uuid4()) is None


async def test_get_by_name_global(db_session):
    repo = RoleRepository(db_session)
    role = _make_role(tenant_id=None)
    await repo.create(role)
    fetched = await repo.get_by_name(role.name, tenant_id=None)
    assert fetched is not None
    assert fetched.id == role.id


async def test_get_by_name_tenant_scoped(db_session):
    repo = RoleRepository(db_session)
    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()

    role = _make_role(tenant_id=tenant.id)
    await repo.create(role)
    fetched = await repo.get_by_name(role.name, tenant_id=tenant.id)
    assert fetched is not None
    assert fetched.id == role.id

    # Same name in global scope should not match
    assert await repo.get_by_name(role.name, tenant_id=None) is None


async def test_list_by_tenant(db_session):
    repo = RoleRepository(db_session)
    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()

    r1 = _make_role(tenant_id=tenant.id)
    r2 = _make_role(tenant_id=tenant.id)
    await repo.create(r1)
    await repo.create(r2)

    roles = await repo.list_by_tenant(tenant.id)
    role_ids = [r.id for r in roles]
    assert r1.id in role_ids
    assert r2.id in role_ids


async def test_update_role(db_session):
    repo = RoleRepository(db_session)
    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()

    role = _make_role(tenant_id=tenant.id)
    await repo.create(role)
    role.description = "updated description"
    updated = await repo.update(role)
    assert updated.description == "updated description"


async def test_delete_role(db_session):
    repo = RoleRepository(db_session)
    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()

    role = _make_role(tenant_id=tenant.id)
    await repo.create(role)
    assert await repo.delete(role.id) is True
    assert await repo.get(role.id) is None


async def test_delete_role_not_found(db_session):
    repo = RoleRepository(db_session)
    assert await repo.delete(uuid.uuid4()) is False


async def test_add_and_get_permissions(db_session):
    repo = RoleRepository(db_session)
    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()

    role = _make_role(tenant_id=tenant.id)
    await repo.create(role)

    perm = _make_permission()
    db_session.add(perm)
    await db_session.flush()

    mapping = await repo.add_permission(role.id, perm.id)
    assert mapping.role_id == role.id
    assert mapping.permission_id == perm.id

    perms = await repo.get_permissions(role.id)
    assert len(perms) == 1
    assert perms[0].id == perm.id


async def test_add_permission_duplicate(db_session):
    repo = RoleRepository(db_session)
    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()

    role = _make_role(tenant_id=tenant.id)
    await repo.create(role)
    perm = _make_permission()
    db_session.add(perm)
    await db_session.flush()

    await repo.add_permission(role.id, perm.id)
    with pytest.raises(RepositoryConflictError):
        await repo.add_permission(role.id, perm.id)


async def test_remove_permission(db_session):
    repo = RoleRepository(db_session)
    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()

    role = _make_role(tenant_id=tenant.id)
    await repo.create(role)
    perm = _make_permission()
    db_session.add(perm)
    await db_session.flush()

    await repo.add_permission(role.id, perm.id)
    assert await repo.remove_permission(role.id, perm.id) is True

    perms = await repo.get_permissions(role.id)
    assert len(perms) == 0


async def test_remove_permission_not_found(db_session):
    repo = RoleRepository(db_session)
    assert await repo.remove_permission(uuid.uuid4(), uuid.uuid4()) is False
