"""Unit tests for RoleService domain orchestration (Story 2.2).

Tests cover:
- create_role: persist, commit, sync; duplicate → Conflict; sync failure → Ok
- get_role: found → Ok(dict); not found → NotFound
- map_permission_to_role: success; not found; duplicate → Conflict
- assign_role_to_user: success; not found; duplicate → Conflict; sync fail → Ok
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from expression import Error, Ok

from app.errors.identity import Conflict, NotFound
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Permission, Role, RolePermission
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.repositories.user import RepositoryConflictError
from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.role import RoleService

TENANT_ID = uuid.uuid4()


def _make_role(**overrides) -> Role:
    defaults = {
        "id": uuid.uuid4(),
        "name": "admin",
        "description": "Admin role",
        "tenant_id": None,
    }
    defaults.update(overrides)
    return Role(**defaults)


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
    perm_repo: AsyncMock | None = None,
    assignment_repo: AsyncMock | None = None,
    adapter: AsyncMock | None = None,
) -> tuple[RoleService, AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    if repo is None:
        repo = AsyncMock(spec=RoleRepository)
    if perm_repo is None:
        perm_repo = AsyncMock(spec=PermissionRepository)
    if assignment_repo is None:
        assignment_repo = AsyncMock(spec=UserTenantRoleRepository)
    if adapter is None:
        adapter = AsyncMock(spec=IdentityProviderAdapter)
    service = RoleService(
        repository=repo,
        permission_repository=perm_repo,
        assignment_repository=assignment_repo,
        adapter=adapter,
    )
    return service, repo, perm_repo, assignment_repo, adapter


@pytest.mark.anyio
class TestCreateRole:
    """AC-2.2.1: create_role persists via repo, commits, syncs, returns Ok(dict)."""

    async def test_create_role_success(self):
        service, repo, _perm, _assign, adapter = _build_service()
        repo.get_by_name.return_value = None
        role = _make_role()
        repo.create.return_value = role
        adapter.sync_role.return_value = Ok(None)

        result = await service.create_role(name=role.name, description=role.description)

        assert result.is_ok()
        repo.create.assert_awaited_once()
        repo.commit.assert_awaited_once()
        adapter.sync_role.assert_awaited_once()

    async def test_create_role_with_tenant(self):
        service, repo, _perm, _assign, adapter = _build_service()
        repo.get_by_name.return_value = None
        role = _make_role(tenant_id=TENANT_ID)
        repo.create.return_value = role
        adapter.sync_role.return_value = Ok(None)

        result = await service.create_role(name=role.name, description=role.description, tenant_id=TENANT_ID)

        assert result.is_ok()
        repo.get_by_name.assert_awaited_once_with(role.name, TENANT_ID)

    async def test_create_role_commit_before_sync(self):
        service, repo, _perm, _assign, adapter = _build_service()
        repo.get_by_name.return_value = None
        role = _make_role()
        repo.create.return_value = role
        adapter.sync_role.return_value = Ok(None)

        call_order = []
        repo.commit.side_effect = lambda: call_order.append("commit")
        adapter.sync_role.side_effect = lambda **kw: (call_order.append("sync"), Ok(None))[1]

        await service.create_role(name=role.name)

        assert call_order == ["commit", "sync"]

    async def test_create_role_duplicate_name_returns_conflict(self):
        """AC-2.2.5: duplicate name within scope → Conflict."""
        service, repo, _perm, _assign, _adapter = _build_service()
        repo.get_by_name.return_value = _make_role()

        result = await service.create_role(name="admin")

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        repo.create.assert_not_awaited()

    async def test_create_role_integrity_error_returns_conflict(self):
        """TOCTOU race: get_by_name returns None but flush raises IntegrityError."""
        service, repo, _perm, _assign, _adapter = _build_service()
        repo.get_by_name.return_value = None
        repo.create.side_effect = RepositoryConflictError("duplicate key")

        result = await service.create_role(name="admin")

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_create_role_sync_failure_still_returns_ok(self):
        service, repo, _perm, _assign, adapter = _build_service()
        repo.get_by_name.return_value = None
        role = _make_role()
        repo.create.return_value = role
        adapter.sync_role.return_value = Error(SyncError(message="Descope down", operation="sync_role"))

        with patch("app.services.role.logger") as mock_logger:
            result = await service.create_role(name=role.name)

        assert result.is_ok()
        repo.commit.assert_awaited_once()
        mock_logger.warning.assert_called_once()


@pytest.mark.anyio
class TestGetRole:
    async def test_get_role_found(self):
        service, repo, _perm, _assign, _adapter = _build_service()
        role = _make_role()
        repo.get.return_value = role

        result = await service.get_role(role_id=role.id)

        assert result.is_ok()
        assert result.ok["name"] == role.name

    async def test_get_role_not_found(self):
        service, repo, _perm, _assign, _adapter = _build_service()
        repo.get.return_value = None

        result = await service.get_role(role_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)


@pytest.mark.anyio
class TestMapPermissionToRole:
    """AC-2.2.2: map_permission_to_role creates mapping, syncs role with permissions."""

    async def test_map_permission_success(self):
        service, repo, perm_repo, _assign, adapter = _build_service()
        role = _make_role()
        perm = _make_permission()
        mapping = RolePermission(role_id=role.id, permission_id=perm.id)

        repo.get.return_value = role
        perm_repo.get.return_value = perm
        repo.add_permission.return_value = mapping
        repo.get_permissions.return_value = [perm]
        adapter.sync_role.return_value = Ok(None)

        result = await service.map_permission_to_role(role_id=role.id, permission_id=perm.id)

        assert result.is_ok()
        assert result.ok["role_id"] == str(role.id)
        assert result.ok["permission_id"] == str(perm.id)
        repo.commit.assert_awaited_once()
        adapter.sync_role.assert_awaited_once()

    async def test_map_permission_fetches_permissions_before_commit(self):
        """get_permissions must happen before commit to avoid post-commit query issues."""
        service, repo, perm_repo, _assign, adapter = _build_service()
        role = _make_role()
        perm = _make_permission()
        mapping = RolePermission(role_id=role.id, permission_id=perm.id)

        repo.get.return_value = role
        perm_repo.get.return_value = perm
        repo.add_permission.return_value = mapping
        repo.get_permissions.return_value = [perm]
        adapter.sync_role.return_value = Ok(None)

        call_order = []
        repo.get_permissions.side_effect = lambda rid: (call_order.append("get_perms"), [perm])[1]
        repo.commit.side_effect = lambda: call_order.append("commit")
        adapter.sync_role.side_effect = lambda **kw: (call_order.append("sync"), Ok(None))[1]

        await service.map_permission_to_role(role_id=role.id, permission_id=perm.id)

        assert call_order == ["get_perms", "commit", "sync"]

    async def test_map_permission_role_not_found(self):
        service, repo, _perm_repo, _assign, _adapter = _build_service()
        repo.get.return_value = None

        result = await service.map_permission_to_role(role_id=uuid.uuid4(), permission_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "Role" in result.error.message

    async def test_map_permission_permission_not_found(self):
        service, repo, perm_repo, _assign, _adapter = _build_service()
        repo.get.return_value = _make_role()
        perm_repo.get.return_value = None

        result = await service.map_permission_to_role(role_id=uuid.uuid4(), permission_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "Permission" in result.error.message

    async def test_map_permission_duplicate_returns_conflict(self):
        service, repo, perm_repo, _assign, _adapter = _build_service()
        repo.get.return_value = _make_role()
        perm_repo.get.return_value = _make_permission()
        repo.add_permission.side_effect = RepositoryConflictError("duplicate mapping")

        result = await service.map_permission_to_role(role_id=uuid.uuid4(), permission_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_map_permission_sync_includes_permission_names(self):
        """Sync payload should include all permission names for the role."""
        service, repo, perm_repo, _assign, adapter = _build_service()
        role = _make_role()
        perm1 = _make_permission(name="read")
        perm2 = _make_permission(name="write")
        mapping = RolePermission(role_id=role.id, permission_id=perm1.id)

        repo.get.return_value = role
        perm_repo.get.return_value = perm1
        repo.add_permission.return_value = mapping
        repo.get_permissions.return_value = [perm1, perm2]
        adapter.sync_role.return_value = Ok(None)

        await service.map_permission_to_role(role_id=role.id, permission_id=perm1.id)

        sync_call = adapter.sync_role.call_args
        assert sync_call.kwargs["data"]["permission_names"] == ["read", "write"]

    async def test_map_permission_sync_failure_still_returns_ok(self):
        """AC-2.4.2: sync failure on map_permission → log warning, still return Ok."""
        service, repo, perm_repo, _assign, adapter = _build_service()
        role = _make_role()
        perm = _make_permission()
        mapping = RolePermission(role_id=role.id, permission_id=perm.id)

        repo.get.return_value = role
        perm_repo.get.return_value = perm
        repo.add_permission.return_value = mapping
        repo.get_permissions.return_value = [perm]
        adapter.sync_role.return_value = Error(SyncError(message="Descope down", operation="sync_role"))

        with patch("app.services.role.logger") as mock_logger:
            result = await service.map_permission_to_role(role_id=role.id, permission_id=perm.id)

        assert result.is_ok()
        repo.commit.assert_awaited_once()
        mock_logger.warning.assert_called_once()


@pytest.mark.anyio
class TestAssignRoleToUser:
    """AC-2.2.4: assign_role_to_user creates assignment, syncs via adapter."""

    async def test_assign_role_success(self):
        service, repo, _perm, assign_repo, adapter = _build_service()
        role = _make_role()
        user_id = uuid.uuid4()
        assignment = UserTenantRole(user_id=user_id, tenant_id=TENANT_ID, role_id=role.id)

        repo.get.return_value = role
        assign_repo.get.return_value = None
        assign_repo.create.return_value = assignment
        adapter.sync_role_assignment.return_value = Ok(None)

        result = await service.assign_role_to_user(user_id=user_id, tenant_id=TENANT_ID, role_id=role.id)

        assert result.is_ok()
        assert result.ok["user_id"] == str(user_id)
        assert result.ok["tenant_id"] == str(TENANT_ID)
        assert result.ok["role_id"] == str(role.id)
        assign_repo.commit.assert_awaited_once()
        adapter.sync_role_assignment.assert_awaited_once()
        sync_call = adapter.sync_role_assignment.call_args
        assert sync_call.kwargs["data"] == {"role_name": role.name}

    async def test_assign_role_not_found(self):
        service, repo, _perm, _assign, _adapter = _build_service()
        repo.get.return_value = None

        result = await service.assign_role_to_user(user_id=uuid.uuid4(), tenant_id=TENANT_ID, role_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)

    async def test_assign_role_existing_assignment_returns_conflict(self):
        service, repo, _perm, assign_repo, _adapter = _build_service()
        role = _make_role()
        repo.get.return_value = role
        assign_repo.get.return_value = UserTenantRole(user_id=uuid.uuid4(), tenant_id=TENANT_ID, role_id=role.id)

        result = await service.assign_role_to_user(user_id=uuid.uuid4(), tenant_id=TENANT_ID, role_id=role.id)

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assign_repo.create.assert_not_awaited()

    async def test_assign_role_integrity_error_returns_conflict(self):
        """TOCTOU race: get returns None but create raises IntegrityError."""
        service, repo, _perm, assign_repo, _adapter = _build_service()
        repo.get.return_value = _make_role()
        assign_repo.get.return_value = None
        assign_repo.create.side_effect = RepositoryConflictError("duplicate assignment")

        result = await service.assign_role_to_user(user_id=uuid.uuid4(), tenant_id=TENANT_ID, role_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_assign_role_sync_failure_still_returns_ok(self):
        service, repo, _perm, assign_repo, adapter = _build_service()
        role = _make_role()
        user_id = uuid.uuid4()
        assignment = UserTenantRole(user_id=user_id, tenant_id=TENANT_ID, role_id=role.id)

        repo.get.return_value = role
        assign_repo.get.return_value = None
        assign_repo.create.return_value = assignment
        adapter.sync_role_assignment.return_value = Error(
            SyncError(message="Descope down", operation="sync_role_assignment")
        )

        with patch("app.services.role.logger") as mock_logger:
            result = await service.assign_role_to_user(user_id=user_id, tenant_id=TENANT_ID, role_id=role.id)

        assert result.is_ok()
        assign_repo.commit.assert_awaited_once()
        mock_logger.warning.assert_called_once()

    async def test_assign_role_with_assigned_by(self):
        service, repo, _perm, assign_repo, adapter = _build_service()
        role = _make_role()
        user_id = uuid.uuid4()
        assigner_id = uuid.uuid4()
        assignment = UserTenantRole(
            user_id=user_id,
            tenant_id=TENANT_ID,
            role_id=role.id,
            assigned_by=assigner_id,
        )

        repo.get.return_value = role
        assign_repo.get.return_value = None
        assign_repo.create.return_value = assignment
        adapter.sync_role_assignment.return_value = Ok(None)

        result = await service.assign_role_to_user(
            user_id=user_id,
            tenant_id=TENANT_ID,
            role_id=role.id,
            assigned_by=assigner_id,
        )

        assert result.is_ok()
        assert result.ok["assigned_by"] == str(assigner_id)


@pytest.mark.anyio
class TestListRoles:
    """Story 2.3: list_roles delegates to repository."""

    async def test_list_roles_returns_list(self):
        service, repo, _perm, _assign, _adapter = _build_service()
        roles = [_make_role(name="admin"), _make_role(name="viewer")]
        repo.list_by_tenant.return_value = roles

        result = await service.list_roles()

        assert result.is_ok()
        assert len(result.ok) == 2
        repo.list_by_tenant.assert_awaited_once_with(None)

    async def test_list_roles_with_tenant(self):
        service, repo, _perm, _assign, _adapter = _build_service()
        repo.list_by_tenant.return_value = []

        result = await service.list_roles(tenant_id=TENANT_ID)

        assert result.is_ok()
        repo.list_by_tenant.assert_awaited_once_with(TENANT_ID)

    async def test_list_roles_empty(self):
        service, repo, _perm, _assign, _adapter = _build_service()
        repo.list_by_tenant.return_value = []

        result = await service.list_roles()

        assert result.is_ok()
        assert result.ok == []


@pytest.mark.anyio
class TestUpdateRole:
    """Story 2.3: update_role updates fields, commits, syncs."""

    async def test_update_role_success(self):
        service, repo, _perm, _assign, adapter = _build_service()
        role = _make_role(name="editor")
        repo.get.return_value = role
        repo.get_by_name.return_value = None
        repo.update.return_value = role
        adapter.sync_role.return_value = Ok(None)

        result = await service.update_role(role_id=role.id, name="senior-editor", description="Senior")

        assert result.is_ok()
        repo.update.assert_awaited_once()
        repo.commit.assert_awaited_once()
        adapter.sync_role.assert_awaited_once()

    async def test_update_role_not_found(self):
        service, repo, _perm, _assign, _adapter = _build_service()
        repo.get.return_value = None

        result = await service.update_role(role_id=uuid.uuid4(), name="new-name")

        assert result.is_error()
        assert isinstance(result.error, NotFound)

    async def test_update_role_name_conflict(self):
        service, repo, _perm, _assign, _adapter = _build_service()
        role = _make_role(name="editor")
        existing = _make_role(name="admin")
        repo.get.return_value = role
        repo.get_by_name.return_value = existing

        result = await service.update_role(role_id=role.id, name="admin")

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_update_role_same_name_no_conflict(self):
        service, repo, _perm, _assign, adapter = _build_service()
        role = _make_role(name="editor")
        repo.get.return_value = role
        repo.get_by_name.return_value = role  # same role
        repo.update.return_value = role
        adapter.sync_role.return_value = Ok(None)

        result = await service.update_role(role_id=role.id, name="editor")

        assert result.is_ok()

    async def test_update_role_integrity_error(self):
        service, repo, _perm, _assign, _adapter = _build_service()
        role = _make_role()
        repo.get.return_value = role
        repo.get_by_name.return_value = None
        repo.update.side_effect = RepositoryConflictError("duplicate key")

        result = await service.update_role(role_id=role.id, name="new-name")

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_update_role_sync_failure_still_returns_ok(self):
        """AC-2.4.2: sync failure on update → log warning, still return Ok."""
        service, repo, _perm, _assign, adapter = _build_service()
        role = _make_role(name="editor")
        repo.get.return_value = role
        repo.get_by_name.return_value = None
        repo.update.return_value = role
        adapter.sync_role.return_value = Error(SyncError(message="Descope down", operation="sync_role"))

        with patch("app.services.role.logger") as mock_logger:
            result = await service.update_role(role_id=role.id, name="senior-editor")

        assert result.is_ok()
        repo.commit.assert_awaited_once()
        mock_logger.warning.assert_called_once()


@pytest.mark.anyio
class TestDeleteRole:
    """Story 2.3: delete_role removes from DB, commits, syncs deletion."""

    async def test_delete_role_success(self):
        service, repo, _perm, _assign, adapter = _build_service()
        role = _make_role(name="editor")
        repo.get.return_value = role
        repo.delete.return_value = True
        adapter.delete_role.return_value = Ok(None)

        result = await service.delete_role(role_id=role.id)

        assert result.is_ok()
        assert result.ok["status"] == "deleted"
        assert result.ok["name"] == "editor"
        repo.commit.assert_awaited_once()
        adapter.delete_role.assert_awaited_once()

    async def test_delete_role_not_found(self):
        service, repo, _perm, _assign, _adapter = _build_service()
        repo.get.return_value = None

        result = await service.delete_role(role_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)

    async def test_delete_role_sync_failure_still_ok(self):
        service, repo, _perm, _assign, adapter = _build_service()
        role = _make_role()
        repo.get.return_value = role
        repo.delete.return_value = True
        adapter.delete_role.return_value = Error(SyncError(message="down", operation="delete_role"))

        with patch("app.services.role.logger") as mock_logger:
            result = await service.delete_role(role_id=role.id)

        assert result.is_ok()
        mock_logger.warning.assert_called_once()


@pytest.mark.anyio
class TestUnassignRoleFromUser:
    """Story 2.3: unassign_role_from_user removes assignment record."""

    async def test_unassign_success(self):
        service, repo, _perm, assign_repo, adapter = _build_service()
        user_id = uuid.uuid4()
        role = _make_role()
        repo.get.return_value = role
        assign_repo.delete.return_value = True
        adapter.delete_role_assignment.return_value = Ok(None)

        result = await service.unassign_role_from_user(user_id=user_id, tenant_id=TENANT_ID, role_id=role.id)

        assert result.is_ok()
        assert result.ok["status"] == "removed"
        assign_repo.commit.assert_awaited_once()
        adapter.delete_role_assignment.assert_awaited_once()

    async def test_unassign_not_found(self):
        service, repo, _perm, assign_repo, _adapter = _build_service()
        repo.get.return_value = None
        assign_repo.delete.return_value = False

        result = await service.unassign_role_from_user(user_id=uuid.uuid4(), tenant_id=TENANT_ID, role_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)

    async def test_unassign_sync_failure_still_returns_ok(self):
        service, repo, _perm, assign_repo, adapter = _build_service()
        role = _make_role()
        repo.get.return_value = role
        assign_repo.delete.return_value = True
        adapter.delete_role_assignment.return_value = Error(
            SyncError(message="Descope down", operation="delete_role_assignment")
        )

        with patch("app.services.role.logger") as mock_logger:
            result = await service.unassign_role_from_user(user_id=uuid.uuid4(), tenant_id=TENANT_ID, role_id=role.id)

        assert result.is_ok()
        assign_repo.commit.assert_awaited_once()
        mock_logger.warning.assert_called_once()
