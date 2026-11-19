"""Decision functions of the Access context: plain values in, Result out."""

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from expression import Error, Ok

from app.domain import access

EARLIER = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = EARLIER + timedelta(days=30)
TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
ADMIN_ID = uuid.uuid4()


def _role(name: str = "editor", tenant_id: uuid.UUID | None = None) -> access.Role:
    return access.Role(
        id=uuid.uuid4(),
        name=access.RoleName(name),
        description="Edits documents",
        tenant_id=tenant_id,
        created_at=EARLIER,
        updated_at=EARLIER,
    )


def _permission(name: str = "documents.read") -> access.Permission:
    return access.Permission(
        id=uuid.uuid4(),
        name=access.PermissionName(name),
        description="Read documents",
        created_at=EARLIER,
        updated_at=EARLIER,
    )


# --- roles --------------------------------------------------------------------


def test_create_role_returns_the_role_and_its_event():
    role_id = uuid.uuid4()
    command = access.CreateRole(name=access.RoleName("editor"), description="Edits", tenant_id=TENANT_ID)

    result = access.create_role(None, command, role_id=role_id, now=NOW)

    role = access.Role(
        id=role_id,
        name=access.RoleName("editor"),
        description="Edits",
        tenant_id=TENANT_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    event = access.RoleCreated(
        occurred_at=NOW, role_id=role_id, name="editor", description="Edits", tenant_id=TENANT_ID
    )
    assert result == Ok((role, event))


def test_create_role_rejects_a_name_taken_in_its_scope():
    holder = _role("editor", TENANT_ID)
    command = access.CreateRole(name=access.RoleName("editor"), description="", tenant_id=TENANT_ID)

    result = access.create_role(holder, command, role_id=uuid.uuid4(), now=NOW)

    assert result == Error(access.RoleNameTaken(name="editor"))


def test_update_role_changes_only_the_fields_given():
    role = _role("editor", TENANT_ID)
    command = access.UpdateRole(role_id=role.id, name=None, description="New description")

    result = access.update_role(role, None, command, now=NOW)

    updated = dataclasses.replace(role, description="New description", updated_at=NOW)
    event = access.RoleUpdated(
        occurred_at=NOW, role_id=role.id, name="editor", description="New description", tenant_id=TENANT_ID
    )
    assert result == Ok((updated, event))


def test_update_role_renames():
    role = _role("editor")
    command = access.UpdateRole(role_id=role.id, name=access.RoleName("senior-editor"), description=None)

    result = access.update_role(role, None, command, now=NOW)

    assert result.is_ok()
    updated, event = result.ok
    assert updated == dataclasses.replace(role, name=access.RoleName("senior-editor"), updated_at=NOW)
    assert event.name == "senior-editor"


def test_update_role_leaves_the_original_untouched():
    role = _role("editor")
    before = dataclasses.replace(role)

    access.update_role(role, None, access.UpdateRole(role_id=role.id, name=None, description="x"), now=NOW)

    assert role == before


def test_update_role_keeping_its_own_name_is_not_a_conflict():
    role = _role("editor")
    command = access.UpdateRole(role_id=role.id, name=access.RoleName("editor"), description=None)

    assert access.update_role(role, role, command, now=NOW).is_ok()


def test_update_role_rejects_a_name_held_by_another_role():
    role = _role("editor")
    holder = _role("admin")
    command = access.UpdateRole(role_id=role.id, name=access.RoleName("admin"), description=None)

    assert access.update_role(role, holder, command, now=NOW) == Error(access.RoleNameTaken(name="admin"))


def test_update_role_rejects_a_missing_role():
    role_id = uuid.uuid4()
    command = access.UpdateRole(role_id=role_id, name=None, description="x")

    assert access.update_role(None, None, command, now=NOW) == Error(access.RoleNotFound(role_id=role_id))


def test_delete_role_returns_its_event():
    role = _role("editor", TENANT_ID)

    result = access.delete_role(role, access.DeleteRole(role_id=role.id), now=NOW)

    assert result == Ok(access.RoleDeleted(occurred_at=NOW, role_id=role.id, name="editor", tenant_id=TENANT_ID))


def test_delete_role_rejects_a_missing_role():
    role_id = uuid.uuid4()

    result = access.delete_role(None, access.DeleteRole(role_id=role_id), now=NOW)

    assert result == Error(access.RoleNotFound(role_id=role_id))


# --- permissions on a role ----------------------------------------------------


def test_map_permission_returns_its_event():
    role = _role()
    permission = _permission("documents.read")
    command = access.MapPermission(role_id=role.id, permission_id=permission.id)

    result = access.map_permission(role, permission, command, now=NOW)

    assert result == Ok(
        access.PermissionMappedToRole(
            occurred_at=NOW, role_id=role.id, permission_id=permission.id, permission_name="documents.read"
        )
    )


def test_map_permission_rejects_a_missing_role():
    permission = _permission()
    command = access.MapPermission(role_id=uuid.uuid4(), permission_id=permission.id)

    result = access.map_permission(None, permission, command, now=NOW)

    assert result == Error(access.RoleNotFound(role_id=command.role_id))


def test_map_permission_rejects_a_missing_permission():
    role = _role()
    command = access.MapPermission(role_id=role.id, permission_id=uuid.uuid4())

    result = access.map_permission(role, None, command, now=NOW)

    assert result == Error(access.PermissionNotFound(permission_id=command.permission_id))


def test_map_permission_reports_the_missing_role_first():
    command = access.MapPermission(role_id=uuid.uuid4(), permission_id=uuid.uuid4())

    assert access.map_permission(None, None, command, now=NOW) == Error(access.RoleNotFound(role_id=command.role_id))


def test_map_named_permissions_returns_an_event_per_name_in_request_order():
    role = _role()
    read, write = _permission("documents.read"), _permission("documents.write")
    command = access.MapNamedPermissions(role_id=role.id, names=("documents.write", "documents.read"))

    result = access.map_named_permissions(role, (read, write), command, now=NOW)

    assert result == Ok(
        (
            access.PermissionMappedToRole(
                occurred_at=NOW, role_id=role.id, permission_id=write.id, permission_name="documents.write"
            ),
            access.PermissionMappedToRole(
                occurred_at=NOW, role_id=role.id, permission_id=read.id, permission_name="documents.read"
            ),
        )
    )


def test_map_named_permissions_with_no_names_maps_nothing():
    role = _role()

    result = access.map_named_permissions(role, (), access.MapNamedPermissions(role_id=role.id, names=()), now=NOW)

    assert result == Ok(())


def test_map_named_permissions_rejects_the_first_unknown_name():
    role = _role()
    command = access.MapNamedPermissions(role_id=role.id, names=("documents.read", "missing.one", "missing.two"))

    result = access.map_named_permissions(role, (_permission("documents.read"),), command, now=NOW)

    assert result == Error(access.PermissionNameNotFound(name="missing.one"))


# --- assignments --------------------------------------------------------------


def test_assign_role_returns_the_assignment_and_its_event():
    role = _role("editor")
    command = access.AssignRole(user_id=USER_ID, tenant_id=TENANT_ID, role_id=role.id, assigned_by=ADMIN_ID)

    result = access.assign_role(role, None, command, now=NOW)

    assignment = access.Assignment(
        user_id=USER_ID, tenant_id=TENANT_ID, role_id=role.id, assigned_by=ADMIN_ID, assigned_at=NOW
    )
    event = access.RoleAssigned(
        occurred_at=NOW,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        role_id=role.id,
        role_name="editor",
        assigned_by=ADMIN_ID,
    )
    assert result == Ok((assignment, event))


def test_assign_role_rejects_a_missing_role():
    command = access.AssignRole(user_id=USER_ID, tenant_id=TENANT_ID, role_id=uuid.uuid4(), assigned_by=None)

    assert access.assign_role(None, None, command, now=NOW) == Error(access.RoleNotFound(role_id=command.role_id))


def test_assign_role_accepts_a_role_scoped_to_the_same_tenant():
    role = _role("editor", TENANT_ID)
    command = access.AssignRole(user_id=USER_ID, tenant_id=TENANT_ID, role_id=role.id, assigned_by=None)

    assert access.assign_role(role, None, command, now=NOW).is_ok()


def test_assign_role_rejects_a_role_scoped_to_another_tenant():
    role = _role("editor", uuid.uuid4())
    command = access.AssignRole(user_id=USER_ID, tenant_id=TENANT_ID, role_id=role.id, assigned_by=None)

    result = access.assign_role(role, None, command, now=NOW)

    assert result == Error(access.RoleOutOfScope(role_id=role.id, tenant_id=TENANT_ID))


def test_assign_role_rejects_a_role_the_user_already_holds():
    role = _role()
    command = access.AssignRole(user_id=USER_ID, tenant_id=TENANT_ID, role_id=role.id, assigned_by=None)
    held = access.Assignment(
        user_id=USER_ID, tenant_id=TENANT_ID, role_id=role.id, assigned_by=None, assigned_at=EARLIER
    )

    result = access.assign_role(role, held, command, now=NOW)

    assert result == Error(access.RoleAlreadyAssigned(user_id=USER_ID, tenant_id=TENANT_ID, role_id=role.id))


def test_unassign_role_returns_its_event():
    command = access.UnassignRole(user_id=USER_ID, tenant_id=TENANT_ID, role_id=uuid.uuid4())

    result = access.unassign_role(True, command, now=NOW)

    assert result == Ok(
        access.RoleUnassigned(occurred_at=NOW, user_id=USER_ID, tenant_id=TENANT_ID, role_id=command.role_id)
    )


def test_unassign_role_rejects_an_assignment_that_was_not_there():
    command = access.UnassignRole(user_id=USER_ID, tenant_id=TENANT_ID, role_id=uuid.uuid4())

    result = access.unassign_role(False, command, now=NOW)

    assert result == Error(access.AssignmentNotFound(user_id=USER_ID, tenant_id=TENANT_ID, role_id=command.role_id))


# --- permissions --------------------------------------------------------------


def test_create_permission_returns_the_permission_and_its_event():
    permission_id = uuid.uuid4()
    command = access.CreatePermission(name=access.PermissionName("documents.read"), description="Read")

    result = access.create_permission(None, command, permission_id=permission_id, now=NOW)

    permission = access.Permission(
        id=permission_id,
        name=access.PermissionName("documents.read"),
        description="Read",
        created_at=NOW,
        updated_at=NOW,
    )
    event = access.PermissionCreated(
        occurred_at=NOW, permission_id=permission_id, name="documents.read", description="Read"
    )
    assert result == Ok((permission, event))


def test_create_permission_rejects_a_taken_name():
    command = access.CreatePermission(name=access.PermissionName("documents.read"), description="")

    result = access.create_permission(_permission("documents.read"), command, permission_id=uuid.uuid4(), now=NOW)

    assert result == Error(access.PermissionNameTaken(name="documents.read"))


def test_update_permission_changes_only_the_fields_given():
    permission = _permission("documents.read")
    command = access.UpdatePermission(permission_id=permission.id, name=None, description="New")

    result = access.update_permission(permission, None, command, now=NOW)

    updated = dataclasses.replace(permission, description="New", updated_at=NOW)
    event = access.PermissionUpdated(
        occurred_at=NOW, permission_id=permission.id, name="documents.read", description="New"
    )
    assert result == Ok((updated, event))


def test_update_permission_keeping_its_own_name_is_not_a_conflict():
    permission = _permission("documents.read")
    command = access.UpdatePermission(
        permission_id=permission.id, name=access.PermissionName("documents.read"), description=None
    )

    assert access.update_permission(permission, permission, command, now=NOW).is_ok()


def test_update_permission_rejects_a_name_held_by_another_permission():
    permission = _permission("documents.read")
    command = access.UpdatePermission(
        permission_id=permission.id, name=access.PermissionName("documents.write"), description=None
    )

    result = access.update_permission(permission, _permission("documents.write"), command, now=NOW)

    assert result == Error(access.PermissionNameTaken(name="documents.write"))


def test_update_permission_rejects_a_missing_permission():
    command = access.UpdatePermission(permission_id=uuid.uuid4(), name=None, description="x")

    result = access.update_permission(None, None, command, now=NOW)

    assert result == Error(access.PermissionNotFound(permission_id=command.permission_id))


def test_delete_permission_returns_its_event():
    permission = _permission("documents.read")

    result = access.delete_permission(permission, access.DeletePermission(permission_id=permission.id), now=NOW)

    assert result == Ok(access.PermissionDeleted(occurred_at=NOW, permission_id=permission.id, name="documents.read"))


def test_delete_permission_rejects_a_missing_permission():
    command = access.DeletePermission(permission_id=uuid.uuid4())

    result = access.delete_permission(None, command, now=NOW)

    assert result == Error(access.PermissionNotFound(permission_id=command.permission_id))


# --- immutability -------------------------------------------------------------


def test_values_and_events_are_immutable():
    role = _role()
    event = access.RoleDeleted(occurred_at=NOW, role_id=role.id, name="editor", tenant_id=None)

    with pytest.raises(dataclasses.FrozenInstanceError):
        role.description = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.name = "changed"


# --- act_on_member: a tenant admin's action on one of the tenant's members ---

CALLER = "caller-1"
TARGET = "member-1"
ACTIONS = list(access.MemberAction)


def _act(action: access.MemberAction, target_roles: set[str], caller_roles: set[str], caller_id: str | None = CALLER):
    member = access.TenantMember(user_id=TARGET, roles=frozenset(target_roles))
    command = access.ActOnMember(action=action, caller_id=caller_id, caller_roles=frozenset(caller_roles))
    return access.act_on_member(member, command)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("target_roles", [set(), {"member"}, {"admin"}], ids=["no role here", "member", "admin"])
def test_an_admin_acts_on_a_member_who_is_neither_owner_nor_operator(action, target_roles):
    assert _act(action, target_roles, {"admin"}) == Ok(access.MemberClearance.CLEARED)


@pytest.mark.parametrize("action", ACTIONS)
def test_only_an_owner_acts_on_an_owner(action):
    assert _act(action, {"owner"}, {"admin"}) == Error(access.OwnerRequired(user_id=TARGET))
    assert _act(action, {"owner"}, {"owner"}) == Ok(access.MemberClearance.CLEARED)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("caller_roles", [{"admin"}, {"owner"}], ids=["admin", "owner"])
def test_an_operator_needs_an_operator(action, caller_roles):
    assert _act(action, {"operator", "member"}, caller_roles) == Ok(access.MemberClearance.OPERATOR_REQUIRED)


def test_an_owner_and_operator_needs_an_owner_first():
    assert _act(access.MemberAction.REMOVE, {"owner", "operator"}, {"admin"}) == Error(
        access.OwnerRequired(user_id=TARGET)
    )
    assert _act(access.MemberAction.REMOVE, {"owner", "operator"}, {"owner"}) == Ok(
        access.MemberClearance.OPERATOR_REQUIRED
    )


@pytest.mark.parametrize("action", [access.MemberAction.DEACTIVATE, access.MemberAction.REMOVE])
@pytest.mark.parametrize("roles", [{"owner"}, {"admin"}], ids=["owner", "admin"])
def test_a_caller_neither_deactivates_nor_removes_itself(action, roles):
    assert _act(action, roles, roles, caller_id=TARGET) == Error(access.ActingOnSelf(user_id=TARGET))


def test_a_caller_may_activate_itself():
    assert _act(access.MemberAction.ACTIVATE, {"admin"}, {"admin"}, caller_id=TARGET) == Ok(
        access.MemberClearance.CLEARED
    )


def test_a_caller_with_no_id_is_never_taken_for_the_member():
    assert _act(access.MemberAction.REMOVE, {"member"}, {"admin"}, caller_id=None) == Ok(access.MemberClearance.CLEARED)
