"""Pins what RoleService and PermissionService answer: payloads, messages and sync data.

The services' own suites assert mostly on the kind of result. These pin the exact
values a caller sees, so a change to the layers beneath cannot alter them.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from expression import Ok

from app.errors.identity import Conflict, NotFound
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Permission, Role, RolePermission
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.base import RepositoryConflictError
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.services.adapters.base import IdentityProviderAdapter
from app.services.cache_invalidation import CacheInvalidationPublisher
from app.services.permission import PermissionService
from app.services.role import RoleService

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
UPDATED = datetime(2026, 2, 1, tzinfo=timezone.utc)


def _role_row(name: str = "editor", tenant_id: uuid.UUID | None = TENANT_ID) -> Role:
    return Role(
        id=uuid.uuid4(),
        name=name,
        description="Edits",
        tenant_id=tenant_id,
        created_at=CREATED,
        updated_at=UPDATED,
    )


def _permission_row(name: str = "documents.read") -> Permission:
    return Permission(id=uuid.uuid4(), name=name, description="Read", created_at=CREATED, updated_at=UPDATED)


def _role_payload(row: Role) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "tenant_id": row.tenant_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _permission_payload(row: Permission) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


class _Roles:
    def __init__(self) -> None:
        self.repo = AsyncMock(spec=RoleRepository)
        self.permissions = AsyncMock(spec=PermissionRepository)
        self.assignments = AsyncMock(spec=UserTenantRoleRepository)
        self.adapter = AsyncMock(spec=IdentityProviderAdapter)
        self.publisher = AsyncMock(spec=CacheInvalidationPublisher)
        for sync in ("sync_role", "delete_role", "sync_role_assignment", "delete_role_assignment"):
            getattr(self.adapter, sync).return_value = Ok(None)
        self.service = RoleService(
            repository=self.repo,
            permission_repository=self.permissions,
            assignment_repository=self.assignments,
            adapter=self.adapter,
            publisher=self.publisher,
        )


class _Permissions:
    def __init__(self) -> None:
        self.repo = AsyncMock(spec=PermissionRepository)
        self.adapter = AsyncMock(spec=IdentityProviderAdapter)
        self.publisher = AsyncMock(spec=CacheInvalidationPublisher)
        self.adapter.sync_permission.return_value = Ok(None)
        self.adapter.delete_permission.return_value = Ok(None)
        self.service = PermissionService(repository=self.repo, adapter=self.adapter, publisher=self.publisher)


@pytest.mark.anyio
class TestRolePayloads:
    async def test_create_role(self):
        roles = _Roles()
        row = _role_row()
        roles.repo.get_by_name.return_value = None
        roles.repo.create.return_value = row

        result = await roles.service.create_role(name="editor", description="Edits", tenant_id=TENANT_ID)

        assert result.ok == _role_payload(row)
        roles.adapter.sync_role.assert_awaited_once_with(
            role_id=row.id, data={"name": "editor", "description": "Edits", "tenant_id": str(TENANT_ID)}
        )

    async def test_create_role_persists_what_was_asked_for(self):
        roles = _Roles()
        roles.repo.get_by_name.return_value = None
        roles.repo.create.side_effect = lambda created: created

        result = await roles.service.create_role(name="editor", description="Edits", tenant_id=TENANT_ID)

        (created,) = roles.repo.create.await_args.args
        assert isinstance(created, Role)
        assert (created.name, created.description, created.tenant_id) == ("editor", "Edits", TENANT_ID)
        assert isinstance(created.id, uuid.UUID)
        assert created.created_at.tzinfo is timezone.utc
        assert result.ok == _role_payload(created)

    async def test_create_role_with_permissions(self):
        roles = _Roles()
        row = _role_row()
        read, write = _permission_row("documents.read"), _permission_row("documents.write")
        roles.repo.get_by_name.return_value = None
        roles.repo.create.return_value = row
        roles.permissions.get_by_name.side_effect = lambda name: {"documents.read": read, "documents.write": write}[
            name
        ]

        result = await roles.service.create_role(
            name="editor", description="Edits", permission_names=["documents.read", "documents.write"]
        )

        assert result.ok == _role_payload(row)
        assert [call.args for call in roles.repo.add_permission.await_args_list] == [
            (row.id, read.id),
            (row.id, write.id),
        ]
        roles.adapter.sync_role.assert_awaited_once_with(
            role_id=row.id,
            data={
                "name": "editor",
                "description": "Edits",
                "tenant_id": str(TENANT_ID),
                "permission_names": ["documents.read", "documents.write"],
            },
        )

    async def test_create_role_with_an_unknown_permission_rolls_back(self):
        roles = _Roles()
        roles.repo.get_by_name.return_value = None
        roles.repo.create.return_value = _role_row()
        roles.permissions.get_by_name.return_value = None

        result = await roles.service.create_role(name="editor", permission_names=["missing.one"])

        assert result.error == NotFound(message="Permission 'missing.one' not found")
        roles.repo.rollback.assert_awaited_once()
        roles.repo.commit.assert_not_awaited()
        roles.publisher.publish.assert_not_awaited()

    async def test_create_role_with_an_already_mapped_permission_carries_on(self):
        roles = _Roles()
        row = _role_row()
        roles.repo.get_by_name.return_value = None
        roles.repo.create.return_value = row
        roles.permissions.get_by_name.return_value = _permission_row()
        roles.repo.add_permission.side_effect = RepositoryConflictError("duplicate mapping")

        result = await roles.service.create_role(name="editor", permission_names=["documents.read"])

        assert result.ok == _role_payload(row)
        roles.repo.commit.assert_awaited_once()

    async def test_get_role(self):
        roles = _Roles()
        row = _role_row()
        roles.repo.get.return_value = row

        assert (await roles.service.get_role(role_id=row.id)).ok == _role_payload(row)

    async def test_list_roles(self):
        roles = _Roles()
        rows = [_role_row("admin"), _role_row("viewer")]
        roles.repo.list_by_tenant.return_value = rows

        assert (await roles.service.list_roles(tenant_id=TENANT_ID)).ok == [_role_payload(row) for row in rows]

    async def test_update_role(self):
        roles = _Roles()
        row = _role_row("editor")
        roles.repo.get.return_value = row
        roles.repo.get_by_name.return_value = None
        roles.repo.update.side_effect = lambda updated: updated

        result = await roles.service.update_role(
            role_id=row.id, name="senior-editor", description="Senior", permission_names=["documents.read"]
        )

        assert (row.name, row.description) == ("senior-editor", "Senior")
        assert result.ok == _role_payload(row)
        roles.repo.get_by_name.assert_awaited_once_with("senior-editor", TENANT_ID)
        roles.repo.update.assert_awaited_once_with(row)
        roles.publisher.publish.assert_awaited_once_with(
            entity_type="role", entity_id=row.id, operation="update", tenant_id=TENANT_ID
        )
        roles.adapter.sync_role.assert_awaited_once_with(
            role_id=row.id,
            data={
                "name": "senior-editor",
                "description": "Senior",
                "tenant_id": str(TENANT_ID),
                "previous_name": "editor",
                "permission_names": ["documents.read"],
            },
        )

    async def test_update_role_description_only_skips_the_name_check(self):
        roles = _Roles()
        row = _role_row("editor")
        roles.repo.get.return_value = row
        roles.repo.update.side_effect = lambda updated: updated

        result = await roles.service.update_role(role_id=row.id, description="Senior")

        assert (row.name, row.description) == ("editor", "Senior")
        assert result.ok == _role_payload(row)
        roles.repo.get_by_name.assert_not_awaited()
        roles.adapter.sync_role.assert_awaited_once_with(
            role_id=row.id, data={"name": "editor", "description": "Senior", "tenant_id": str(TENANT_ID)}
        )

    async def test_delete_role(self):
        roles = _Roles()
        row = _role_row("editor")
        roles.repo.get.return_value = row
        roles.repo.delete.return_value = True

        result = await roles.service.delete_role(role_id=row.id)

        assert result.ok == {"status": "deleted", "name": "editor"}
        roles.adapter.delete_role.assert_awaited_once_with(
            role_id=row.id, data={"name": "editor", "tenant_id": str(TENANT_ID)}
        )

    async def test_delete_role_lost_to_a_concurrent_delete(self):
        roles = _Roles()
        row = _role_row()
        roles.repo.get.return_value = row
        roles.repo.delete.return_value = False

        result = await roles.service.delete_role(role_id=row.id)

        assert result.error == NotFound(message=f"Role '{row.id}' not found")
        roles.repo.commit.assert_not_awaited()

    async def test_map_permission(self):
        roles = _Roles()
        row, permission = _role_row(), _permission_row("documents.read")
        roles.repo.get.return_value = row
        roles.permissions.get.return_value = permission
        roles.repo.add_permission.return_value = RolePermission(role_id=row.id, permission_id=permission.id)
        roles.repo.get_permissions.return_value = [permission]

        result = await roles.service.map_permission_to_role(role_id=row.id, permission_id=permission.id)

        assert result.ok == {"role_id": str(row.id), "permission_id": str(permission.id)}
        roles.repo.add_permission.assert_awaited_once_with(row.id, permission.id)
        roles.publisher.publish.assert_awaited_once_with(
            entity_type="role", entity_id=row.id, operation="update", tenant_id=TENANT_ID
        )
        roles.adapter.sync_role.assert_awaited_once_with(
            role_id=row.id,
            data={
                "name": "editor",
                "description": "Edits",
                "tenant_id": str(TENANT_ID),
                "permission_names": ["documents.read"],
            },
        )

    async def test_assign_role(self):
        roles = _Roles()
        row = _role_row("editor")
        admin_id = uuid.uuid4()
        roles.repo.get.return_value = row
        roles.assignments.get.return_value = None
        roles.assignments.create.side_effect = lambda created: created

        result = await roles.service.assign_role_to_user(
            user_id=USER_ID, tenant_id=TENANT_ID, role_id=row.id, assigned_by=admin_id
        )

        (created,) = roles.assignments.create.await_args.args
        assert isinstance(created, UserTenantRole)
        assert result.ok == {
            "user_id": str(USER_ID),
            "tenant_id": str(TENANT_ID),
            "role_id": str(row.id),
            "assigned_by": str(admin_id),
            "assigned_at": created.assigned_at.isoformat(),
        }
        roles.publisher.publish.assert_awaited_once_with(
            entity_type="role", entity_id=row.id, operation="assign", tenant_id=TENANT_ID
        )
        roles.adapter.sync_role_assignment.assert_awaited_once_with(
            user_id=USER_ID, tenant_id=TENANT_ID, role_id=row.id, data={"role_name": "editor"}
        )

    async def test_assign_role_without_an_assigner(self):
        roles = _Roles()
        row = _role_row()
        roles.repo.get.return_value = row
        roles.assignments.get.return_value = None
        roles.assignments.create.side_effect = lambda created: created

        result = await roles.service.assign_role_to_user(user_id=USER_ID, tenant_id=TENANT_ID, role_id=row.id)

        assert result.ok["assigned_by"] is None

    async def test_unassign_role(self):
        roles = _Roles()
        row = _role_row("editor")
        roles.repo.get.return_value = row
        roles.assignments.delete.return_value = True

        result = await roles.service.unassign_role_from_user(user_id=USER_ID, tenant_id=TENANT_ID, role_id=row.id)

        assert result.ok == {
            "status": "removed",
            "user_id": str(USER_ID),
            "tenant_id": str(TENANT_ID),
            "role_id": str(row.id),
        }
        roles.assignments.delete.assert_awaited_once_with(USER_ID, TENANT_ID, row.id)
        roles.publisher.publish.assert_awaited_once_with(
            entity_type="role", entity_id=row.id, operation="unassign", tenant_id=TENANT_ID
        )
        roles.adapter.delete_role_assignment.assert_awaited_once_with(
            user_id=USER_ID, tenant_id=TENANT_ID, role_id=row.id, data={"role_name": "editor"}
        )

    async def test_unassign_a_role_that_no_longer_exists_syncs_its_id_as_the_name(self):
        roles = _Roles()
        role_id = uuid.uuid4()
        roles.repo.get.return_value = None
        roles.assignments.delete.return_value = True

        result = await roles.service.unassign_role_from_user(user_id=USER_ID, tenant_id=TENANT_ID, role_id=role_id)

        assert result.is_ok()
        roles.adapter.delete_role_assignment.assert_awaited_once_with(
            user_id=USER_ID, tenant_id=TENANT_ID, role_id=role_id, data={"role_name": str(role_id)}
        )


@pytest.mark.anyio
class TestRoleMessages:
    async def test_create_role_name_taken(self):
        roles = _Roles()
        roles.repo.get_by_name.return_value = _role_row("editor")

        result = await roles.service.create_role(name="editor")

        assert result.error == Conflict(message="Role 'editor' already exists in this scope")

    async def test_create_role_lost_to_a_concurrent_create(self):
        roles = _Roles()
        roles.repo.get_by_name.return_value = None
        roles.repo.create.side_effect = RepositoryConflictError("duplicate key")

        result = await roles.service.create_role(name="editor")

        assert result.error == Conflict(message="Role 'editor' already exists in this scope")
        roles.repo.rollback.assert_awaited_once()

    async def test_get_role_not_found(self):
        roles = _Roles()
        role_id = uuid.uuid4()
        roles.repo.get.return_value = None

        assert (await roles.service.get_role(role_id=role_id)).error == NotFound(message=f"Role '{role_id}' not found")

    async def test_update_role_not_found(self):
        roles = _Roles()
        role_id = uuid.uuid4()
        roles.repo.get.return_value = None

        result = await roles.service.update_role(role_id=role_id, name="editor")

        assert result.error == NotFound(message=f"Role '{role_id}' not found")

    async def test_update_role_name_taken(self):
        roles = _Roles()
        row = _role_row("editor")
        roles.repo.get.return_value = row
        roles.repo.get_by_name.return_value = _role_row("admin")

        result = await roles.service.update_role(role_id=row.id, name="admin")

        assert result.error == Conflict(message="Role 'admin' already exists in this scope")
        assert row.name == "editor"

    async def test_update_role_lost_to_a_concurrent_rename(self):
        roles = _Roles()
        row = _role_row("editor")
        roles.repo.get.return_value = row
        roles.repo.get_by_name.return_value = None
        roles.repo.update.side_effect = RepositoryConflictError("duplicate key")

        result = await roles.service.update_role(role_id=row.id, name="admin")

        assert result.error == Conflict(message="Role name 'admin' conflicts with existing role")
        roles.repo.rollback.assert_awaited_once()

    async def test_delete_role_not_found(self):
        roles = _Roles()
        role_id = uuid.uuid4()
        roles.repo.get.return_value = None

        result = await roles.service.delete_role(role_id=role_id)

        assert result.error == NotFound(message=f"Role '{role_id}' not found")

    async def test_map_permission_role_not_found(self):
        roles = _Roles()
        role_id = uuid.uuid4()
        roles.repo.get.return_value = None
        roles.permissions.get.return_value = _permission_row()

        result = await roles.service.map_permission_to_role(role_id=role_id, permission_id=uuid.uuid4())

        assert result.error == NotFound(message=f"Role '{role_id}' not found")

    async def test_map_permission_permission_not_found(self):
        roles = _Roles()
        permission_id = uuid.uuid4()
        roles.repo.get.return_value = _role_row()
        roles.permissions.get.return_value = None

        result = await roles.service.map_permission_to_role(role_id=uuid.uuid4(), permission_id=permission_id)

        assert result.error == NotFound(message=f"Permission '{permission_id}' not found")

    async def test_map_permission_already_mapped(self):
        roles = _Roles()
        row, permission = _role_row(), _permission_row()
        roles.repo.get.return_value = row
        roles.permissions.get.return_value = permission
        roles.repo.add_permission.side_effect = RepositoryConflictError("duplicate mapping")

        result = await roles.service.map_permission_to_role(role_id=row.id, permission_id=permission.id)

        assert result.error == Conflict(message=f"Permission '{permission.id}' is already mapped to role '{row.id}'")
        roles.repo.rollback.assert_awaited_once()

    async def test_assign_role_not_found(self):
        roles = _Roles()
        role_id = uuid.uuid4()
        roles.repo.get.return_value = None
        roles.assignments.get.return_value = None

        result = await roles.service.assign_role_to_user(user_id=USER_ID, tenant_id=TENANT_ID, role_id=role_id)

        assert result.error == NotFound(message=f"Role '{role_id}' not found")

    async def test_assign_role_already_held(self):
        roles = _Roles()
        row = _role_row()
        roles.repo.get.return_value = row
        roles.assignments.get.return_value = UserTenantRole(user_id=USER_ID, tenant_id=TENANT_ID, role_id=row.id)

        result = await roles.service.assign_role_to_user(user_id=USER_ID, tenant_id=TENANT_ID, role_id=row.id)

        assert result.error == Conflict(message=f"User '{USER_ID}' already has role '{row.id}' in tenant '{TENANT_ID}'")

    async def test_assign_role_lost_to_a_concurrent_assign(self):
        roles = _Roles()
        row = _role_row()
        roles.repo.get.return_value = row
        roles.assignments.get.return_value = None
        roles.assignments.create.side_effect = RepositoryConflictError("duplicate assignment")

        result = await roles.service.assign_role_to_user(user_id=USER_ID, tenant_id=TENANT_ID, role_id=row.id)

        assert result.error == Conflict(message=f"User '{USER_ID}' already has role '{row.id}' in tenant '{TENANT_ID}'")
        roles.assignments.rollback.assert_awaited_once()

    async def test_unassign_role_not_held(self):
        roles = _Roles()
        roles.repo.get.return_value = None
        roles.assignments.delete.return_value = False

        result = await roles.service.unassign_role_from_user(user_id=USER_ID, tenant_id=TENANT_ID, role_id=uuid.uuid4())

        assert result.error == NotFound(
            message=f"Role assignment not found for user '{USER_ID}' in tenant '{TENANT_ID}'"
        )
        roles.assignments.commit.assert_not_awaited()


@pytest.mark.anyio
class TestPermissionPayloads:
    async def test_create_permission(self):
        permissions = _Permissions()
        row = _permission_row("documents.read")
        permissions.repo.get_by_name.return_value = None
        permissions.repo.create.return_value = row

        result = await permissions.service.create_permission(name="documents.read", description="Read")

        assert result.ok == _permission_payload(row)
        permissions.adapter.sync_permission.assert_awaited_once_with(
            permission_id=row.id, data={"name": "documents.read", "description": "Read"}
        )

    async def test_create_permission_persists_what_was_asked_for(self):
        permissions = _Permissions()
        permissions.repo.get_by_name.return_value = None
        permissions.repo.create.side_effect = lambda created: created

        result = await permissions.service.create_permission(name="documents.read", description="Read")

        (created,) = permissions.repo.create.await_args.args
        assert isinstance(created, Permission)
        assert (created.name, created.description) == ("documents.read", "Read")
        assert result.ok == _permission_payload(created)

    async def test_get_permission(self):
        permissions = _Permissions()
        row = _permission_row()
        permissions.repo.get.return_value = row

        assert (await permissions.service.get_permission(permission_id=row.id)).ok == _permission_payload(row)

    async def test_list_permissions(self):
        permissions = _Permissions()
        rows = [_permission_row("documents.read"), _permission_row("documents.write")]
        permissions.repo.list_all.return_value = rows

        assert (await permissions.service.list_permissions()).ok == [_permission_payload(row) for row in rows]

    async def test_update_permission(self):
        permissions = _Permissions()
        row = _permission_row("documents.read")
        permissions.repo.get.return_value = row
        permissions.repo.get_by_name.return_value = None
        permissions.repo.update.side_effect = lambda updated: updated

        result = await permissions.service.update_permission(
            permission_id=row.id, name="documents.view", description="View"
        )

        assert (row.name, row.description) == ("documents.view", "View")
        assert result.ok == _permission_payload(row)
        permissions.repo.update.assert_awaited_once_with(row)
        permissions.publisher.publish.assert_awaited_once_with(
            entity_type="permission", entity_id=row.id, operation="update"
        )
        permissions.adapter.sync_permission.assert_awaited_once_with(
            permission_id=row.id,
            data={"name": "documents.view", "description": "View", "previous_name": "documents.read"},
        )

    async def test_delete_permission(self):
        permissions = _Permissions()
        row = _permission_row("documents.read")
        permissions.repo.get.return_value = row
        permissions.repo.delete.return_value = True

        result = await permissions.service.delete_permission(permission_id=row.id)

        assert result.ok == {"status": "deleted", "name": "documents.read"}
        permissions.adapter.delete_permission.assert_awaited_once_with(
            permission_id=row.id, data={"name": "documents.read"}
        )


@pytest.mark.anyio
class TestPermissionMessages:
    async def test_create_permission_name_taken(self):
        permissions = _Permissions()
        permissions.repo.get_by_name.return_value = _permission_row("documents.read")

        result = await permissions.service.create_permission(name="documents.read")

        assert result.error == Conflict(message="Permission 'documents.read' already exists")

    async def test_create_permission_lost_to_a_concurrent_create(self):
        permissions = _Permissions()
        permissions.repo.get_by_name.return_value = None
        permissions.repo.create.side_effect = RepositoryConflictError("duplicate key")

        result = await permissions.service.create_permission(name="documents.read")

        assert result.error == Conflict(message="Permission 'documents.read' already exists")
        permissions.repo.rollback.assert_awaited_once()

    async def test_get_permission_not_found(self):
        permissions = _Permissions()
        permission_id = uuid.uuid4()
        permissions.repo.get.return_value = None

        result = await permissions.service.get_permission(permission_id=permission_id)

        assert result.error == NotFound(message=f"Permission '{permission_id}' not found")

    async def test_update_permission_name_taken(self):
        permissions = _Permissions()
        row = _permission_row("documents.read")
        permissions.repo.get.return_value = row
        permissions.repo.get_by_name.return_value = _permission_row("documents.write")

        result = await permissions.service.update_permission(permission_id=row.id, name="documents.write")

        assert result.error == Conflict(message="Permission 'documents.write' already exists")
        assert row.name == "documents.read"

    async def test_update_permission_lost_to_a_concurrent_rename(self):
        permissions = _Permissions()
        row = _permission_row("documents.read")
        permissions.repo.get.return_value = row
        permissions.repo.get_by_name.return_value = None
        permissions.repo.update.side_effect = RepositoryConflictError("duplicate key")

        result = await permissions.service.update_permission(permission_id=row.id, name="documents.write")

        assert result.error == Conflict(message="Permission name 'documents.write' conflicts with existing permission")

    async def test_delete_permission_lost_to_a_concurrent_delete(self):
        permissions = _Permissions()
        row = _permission_row()
        permissions.repo.get.return_value = row
        permissions.repo.delete.return_value = False

        result = await permissions.service.delete_permission(permission_id=row.id)

        assert result.error == NotFound(message=f"Permission '{row.id}' not found")
        permissions.repo.commit.assert_not_awaited()
