"""Unit tests for PermissionRepository — data access layer tests against real Postgres.

Tests cover:
- create: happy path, duplicate name → RepositoryConflictError
- get: found, not found
- get_by_name: found, not found
- list_all: returns all permissions
- update: happy path
- delete: existing, non-existent
"""

import uuid

import pytest

from app.models.identity.role import Permission
from app.repositories.base import RepositoryConflictError
from app.repositories.permission import PermissionRepository

pytestmark = pytest.mark.asyncio


def _make_permission(**overrides) -> Permission:
    defaults = {
        "id": uuid.uuid4(),
        "name": f"perm-{uuid.uuid4().hex[:8]}",
        "description": "test permission",
    }
    defaults.update(overrides)
    return Permission(**defaults)


async def test_create_permission(db_session):
    repo = PermissionRepository(db_session)
    perm = _make_permission()
    created = await repo.create(perm)
    assert created.id == perm.id
    assert created.name == perm.name
    assert created.created_at is not None


async def test_create_permission_duplicate_name(db_session):
    repo = PermissionRepository(db_session)
    name = f"dup-perm-{uuid.uuid4().hex[:8]}"
    await repo.create(_make_permission(name=name))
    with pytest.raises(RepositoryConflictError):
        await repo.create(_make_permission(name=name))


async def test_get_permission(db_session):
    repo = PermissionRepository(db_session)
    perm = _make_permission()
    await repo.create(perm)
    fetched = await repo.get(perm.id)
    assert fetched is not None
    assert fetched.id == perm.id
    assert fetched.name == perm.name


async def test_get_permission_not_found(db_session):
    repo = PermissionRepository(db_session)
    assert await repo.get(uuid.uuid4()) is None


async def test_get_by_name(db_session):
    repo = PermissionRepository(db_session)
    perm = _make_permission()
    await repo.create(perm)
    fetched = await repo.get_by_name(perm.name)
    assert fetched is not None
    assert fetched.id == perm.id


async def test_get_by_name_not_found(db_session):
    repo = PermissionRepository(db_session)
    assert await repo.get_by_name("nonexistent-perm") is None


async def test_list_all(db_session):
    repo = PermissionRepository(db_session)
    p1 = _make_permission()
    p2 = _make_permission()
    await repo.create(p1)
    await repo.create(p2)

    all_perms = await repo.list_all()
    perm_ids = [p.id for p in all_perms]
    assert p1.id in perm_ids
    assert p2.id in perm_ids


async def test_update_permission(db_session):
    repo = PermissionRepository(db_session)
    perm = _make_permission()
    await repo.create(perm)
    perm.description = "updated description"
    updated = await repo.update(perm)
    assert updated.description == "updated description"


async def test_delete_permission(db_session):
    repo = PermissionRepository(db_session)
    perm = _make_permission()
    await repo.create(perm)
    assert await repo.delete(perm.id) is True
    assert await repo.get(perm.id) is None


async def test_delete_permission_not_found(db_session):
    repo = PermissionRepository(db_session)
    assert await repo.delete(uuid.uuid4()) is False
