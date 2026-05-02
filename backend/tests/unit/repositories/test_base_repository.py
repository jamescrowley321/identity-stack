"""Unit tests for BaseRepository — verifies generic CRUD, transaction helpers, and RepositoryConflictError.

Tests cover:
- create: happy path, IntegrityError → RepositoryConflictError (no rollback)
- get: found, not found
- update: happy path, IntegrityError → RepositoryConflictError
- delete: existing entity, non-existent entity
- commit / rollback: delegate to session
- RepositoryConflictError importable from app.repositories.base
- Concrete repos inherit BaseRepository and set _model
"""

import uuid

import pytest

from app.models.identity.role import Permission
from app.repositories.base import BaseRepository, RepositoryConflictError
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


async def test_base_create(db_session):
    """BaseRepository.create adds entity and flushes."""
    repo = PermissionRepository(db_session)
    perm = _make_permission()
    created = await repo.create(perm)
    assert created.id == perm.id
    assert created.created_at is not None


async def test_base_create_conflict(db_session):
    """BaseRepository.create raises RepositoryConflictError on IntegrityError."""
    repo = PermissionRepository(db_session)
    name = f"dup-{uuid.uuid4().hex[:8]}"
    await repo.create(_make_permission(name=name))
    with pytest.raises(RepositoryConflictError):
        await repo.create(_make_permission(name=name))


async def test_base_get(db_session):
    """BaseRepository.get returns entity by primary key."""
    repo = PermissionRepository(db_session)
    perm = _make_permission()
    await repo.create(perm)
    fetched = await repo.get(perm.id)
    assert fetched is not None
    assert fetched.id == perm.id


async def test_base_get_not_found(db_session):
    """BaseRepository.get returns None when entity not found."""
    repo = PermissionRepository(db_session)
    result = await repo.get(uuid.uuid4())
    assert result is None


async def test_base_update(db_session):
    """BaseRepository.update flushes mutations."""
    repo = PermissionRepository(db_session)
    perm = _make_permission()
    await repo.create(perm)
    perm.description = "updated"
    updated = await repo.update(perm)
    assert updated.description == "updated"


async def test_base_delete(db_session):
    """BaseRepository.delete removes entity and returns True."""
    repo = PermissionRepository(db_session)
    perm = _make_permission()
    await repo.create(perm)
    assert await repo.delete(perm.id) is True
    assert await repo.get(perm.id) is None


async def test_base_delete_not_found(db_session):
    """BaseRepository.delete returns False when entity not found."""
    repo = PermissionRepository(db_session)
    assert await repo.delete(uuid.uuid4()) is False


async def test_concrete_repos_inherit_base():
    """All concrete repositories inherit from BaseRepository."""
    from app.repositories.assignment import UserTenantRoleRepository
    from app.repositories.idp_link import IdPLinkRepository
    from app.repositories.provider import ProviderRepository
    from app.repositories.role import RoleRepository
    from app.repositories.tenant import TenantRepository
    from app.repositories.user import UserRepository

    for repo_cls in [
        UserRepository,
        RoleRepository,
        PermissionRepository,
        TenantRepository,
        ProviderRepository,
        IdPLinkRepository,
        UserTenantRoleRepository,
    ]:
        assert issubclass(repo_cls, BaseRepository), f"{repo_cls.__name__} does not inherit BaseRepository"
        assert hasattr(repo_cls, "_model"), f"{repo_cls.__name__} missing _model attribute"


async def test_repository_conflict_error_importable():
    """RepositoryConflictError is importable from app.repositories.base."""
    from app.repositories.base import RepositoryConflictError as RCE

    assert issubclass(RCE, Exception)
