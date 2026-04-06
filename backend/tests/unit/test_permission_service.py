"""Unit tests for PermissionService domain orchestration (Story 2.2).

Tests cover:
- create_permission: persist, commit, sync; duplicate → Conflict; sync fail → Ok
- get_permission: found → Ok(dict); not found → NotFound
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from expression import Error, Ok

from app.errors.identity import Conflict, NotFound
from app.models.identity.role import Permission
from app.repositories.permission import PermissionRepository
from app.repositories.user import RepositoryConflictError
from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.permission import PermissionService


def _make_permission(**overrides) -> Permission:
    defaults = {
        "id": uuid.uuid4(),
        "name": "documents.read",
        "description": "Read documents",
    }
    defaults.update(overrides)
    return Permission(**defaults)


def _build_service(
    repo: AsyncMock | None = None,
    adapter: AsyncMock | None = None,
) -> tuple[PermissionService, AsyncMock, AsyncMock]:
    if repo is None:
        repo = AsyncMock(spec=PermissionRepository)
    if adapter is None:
        adapter = AsyncMock(spec=IdentityProviderAdapter)
    service = PermissionService(repository=repo, adapter=adapter)
    return service, repo, adapter


@pytest.mark.anyio
class TestCreatePermission:
    """AC-2.2.2: create_permission persists via repo, commits, syncs, returns Ok(dict)."""

    async def test_create_permission_success(self):
        service, repo, adapter = _build_service()
        repo.get_by_name.return_value = None
        perm = _make_permission()
        repo.create.return_value = perm
        adapter.sync_permission.return_value = Ok(None)

        result = await service.create_permission(name=perm.name, description=perm.description)

        assert result.is_ok()
        repo.create.assert_awaited_once()
        repo.commit.assert_awaited_once()
        adapter.sync_permission.assert_awaited_once()

    async def test_create_permission_commit_before_sync(self):
        service, repo, adapter = _build_service()
        repo.get_by_name.return_value = None
        perm = _make_permission()
        repo.create.return_value = perm
        adapter.sync_permission.return_value = Ok(None)

        call_order = []
        repo.commit.side_effect = lambda: call_order.append("commit")
        adapter.sync_permission.side_effect = lambda **kw: (call_order.append("sync"), Ok(None))[1]

        await service.create_permission(name=perm.name)

        assert call_order == ["commit", "sync"]

    async def test_create_permission_duplicate_name_returns_conflict(self):
        """AC-2.2.5: duplicate name → Conflict."""
        service, repo, _adapter = _build_service()
        repo.get_by_name.return_value = _make_permission()

        result = await service.create_permission(name="documents.read")

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        repo.create.assert_not_awaited()

    async def test_create_permission_integrity_error_returns_conflict(self):
        """TOCTOU race: get_by_name returns None but flush raises IntegrityError."""
        service, repo, _adapter = _build_service()
        repo.get_by_name.return_value = None
        repo.create.side_effect = RepositoryConflictError("duplicate key")

        result = await service.create_permission(name="documents.read")

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_create_permission_sync_failure_still_returns_ok(self):
        service, repo, adapter = _build_service()
        repo.get_by_name.return_value = None
        perm = _make_permission()
        repo.create.return_value = perm
        adapter.sync_permission.return_value = Error(SyncError(message="Descope down", operation="sync_permission"))

        with patch("app.services.permission.logger") as mock_logger:
            result = await service.create_permission(name=perm.name)

        assert result.is_ok()
        repo.commit.assert_awaited_once()
        mock_logger.warning.assert_called_once()


@pytest.mark.anyio
class TestGetPermission:
    async def test_get_permission_found(self):
        service, repo, _adapter = _build_service()
        perm = _make_permission()
        repo.get.return_value = perm

        result = await service.get_permission(permission_id=perm.id)

        assert result.is_ok()
        assert result.ok["name"] == perm.name

    async def test_get_permission_not_found(self):
        service, repo, _adapter = _build_service()
        repo.get.return_value = None

        result = await service.get_permission(permission_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)


@pytest.mark.anyio
class TestListPermissions:
    """Story 2.3: list_permissions delegates to repository."""

    async def test_list_permissions_returns_list(self):
        service, repo, _adapter = _build_service()
        perms = [_make_permission(name="read"), _make_permission(name="write")]
        repo.list_all.return_value = perms

        result = await service.list_permissions()

        assert result.is_ok()
        assert len(result.ok) == 2
        repo.list_all.assert_awaited_once()

    async def test_list_permissions_empty(self):
        service, repo, _adapter = _build_service()
        repo.list_all.return_value = []

        result = await service.list_permissions()

        assert result.is_ok()
        assert result.ok == []


@pytest.mark.anyio
class TestUpdatePermission:
    """Story 2.3: update_permission updates fields, commits, syncs."""

    async def test_update_permission_success(self):
        service, repo, adapter = _build_service()
        perm = _make_permission(name="reports.read")
        repo.get.return_value = perm
        repo.get_by_name.return_value = None
        repo.update.return_value = perm
        adapter.sync_permission.return_value = Ok(None)

        result = await service.update_permission(permission_id=perm.id, name="reports.view", description="View reports")

        assert result.is_ok()
        repo.update.assert_awaited_once()
        repo.commit.assert_awaited_once()
        adapter.sync_permission.assert_awaited_once()

    async def test_update_permission_not_found(self):
        service, repo, _adapter = _build_service()
        repo.get.return_value = None

        result = await service.update_permission(permission_id=uuid.uuid4(), name="new")

        assert result.is_error()
        assert isinstance(result.error, NotFound)

    async def test_update_permission_name_conflict(self):
        service, repo, _adapter = _build_service()
        perm = _make_permission(name="reports.read")
        existing = _make_permission(name="reports.write")
        repo.get.return_value = perm
        repo.get_by_name.return_value = existing

        result = await service.update_permission(permission_id=perm.id, name="reports.write")

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_update_permission_integrity_error(self):
        service, repo, _adapter = _build_service()
        perm = _make_permission()
        repo.get.return_value = perm
        repo.get_by_name.return_value = None
        repo.update.side_effect = RepositoryConflictError("duplicate key")

        result = await service.update_permission(permission_id=perm.id, name="new")

        assert result.is_error()
        assert isinstance(result.error, Conflict)


@pytest.mark.anyio
class TestDeletePermission:
    """Story 2.3: delete_permission removes from DB, commits, syncs deletion."""

    async def test_delete_permission_success(self):
        service, repo, adapter = _build_service()
        perm = _make_permission(name="reports.read")
        repo.get.return_value = perm
        repo.delete.return_value = True
        adapter.delete_permission.return_value = Ok(None)

        result = await service.delete_permission(permission_id=perm.id)

        assert result.is_ok()
        assert result.ok["status"] == "deleted"
        assert result.ok["name"] == "reports.read"
        repo.commit.assert_awaited_once()
        adapter.delete_permission.assert_awaited_once()

    async def test_delete_permission_not_found(self):
        service, repo, _adapter = _build_service()
        repo.get.return_value = None

        result = await service.delete_permission(permission_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)

    async def test_delete_permission_sync_failure_still_ok(self):
        service, repo, adapter = _build_service()
        perm = _make_permission()
        repo.get.return_value = perm
        repo.delete.return_value = True
        adapter.delete_permission.return_value = Error(SyncError(message="down", operation="delete_permission"))

        with patch("app.services.permission.logger") as mock_logger:
            result = await service.delete_permission(permission_id=perm.id)

        assert result.is_ok()
        mock_logger.warning.assert_called_once()
