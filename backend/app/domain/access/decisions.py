"""Decisions of the Access context.

Each function takes the state it needs and a command, and returns the new value
and the event that records it, or the error that refuses the command. None of
them does I/O: the caller loads the state and supplies ids and the time.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from expression import Error, Ok, Result

from app.domain.access.commands import (
    ActOnMember,
    AssignRole,
    CreatePermission,
    CreateRole,
    DeletePermission,
    DeleteRole,
    MapNamedPermissions,
    MapPermission,
    MemberAction,
    UnassignRole,
    UpdatePermission,
    UpdateRole,
)
from app.domain.access.errors import (
    AccessError,
    ActingOnSelf,
    AssignmentNotFound,
    OwnerRequired,
    PermissionNameNotFound,
    PermissionNameTaken,
    PermissionNotFound,
    RoleAlreadyAssigned,
    RoleNameTaken,
    RoleNotFound,
    RoleOutOfScope,
)
from app.domain.access.events import (
    PermissionCreated,
    PermissionDeleted,
    PermissionMappedToRole,
    PermissionUpdated,
    RoleAssigned,
    RoleCreated,
    RoleDeleted,
    RoleUnassigned,
    RoleUpdated,
)
from app.domain.access.values import (
    OPERATOR_ROLE,
    OWNER_ROLE,
    Assignment,
    MemberClearance,
    Permission,
    Role,
    TenantMember,
)


def create_role(
    name_holder: Role | None,
    command: CreateRole,
    *,
    role_id: uuid.UUID,
    now: datetime,
) -> Result[tuple[Role, RoleCreated], AccessError]:
    """``name_holder`` is the role that already has the name in the command's scope."""
    if name_holder is not None:
        return Error(RoleNameTaken(name=command.name.value))
    role = Role(
        id=role_id,
        name=command.name,
        description=command.description,
        tenant_id=command.tenant_id,
        created_at=now,
        updated_at=now,
    )
    event = RoleCreated(
        occurred_at=now,
        role_id=role.id,
        name=role.name.value,
        description=role.description,
        tenant_id=role.tenant_id,
    )
    return Ok((role, event))


def update_role(
    role: Role | None,
    name_holder: Role | None,
    command: UpdateRole,
    *,
    now: datetime,
) -> Result[tuple[Role, RoleUpdated], AccessError]:
    """``name_holder`` is the role that has the command's new name in ``role``'s scope."""
    if role is None:
        return Error(RoleNotFound(role_id=command.role_id))
    if command.name is not None and name_holder is not None and name_holder.id != role.id:
        return Error(RoleNameTaken(name=command.name.value))
    updated = replace(
        role,
        name=role.name if command.name is None else command.name,
        description=role.description if command.description is None else command.description,
        updated_at=now,
    )
    event = RoleUpdated(
        occurred_at=now,
        role_id=updated.id,
        name=updated.name.value,
        description=updated.description,
        tenant_id=updated.tenant_id,
    )
    return Ok((updated, event))


def delete_role(role: Role | None, command: DeleteRole, *, now: datetime) -> Result[RoleDeleted, AccessError]:
    if role is None:
        return Error(RoleNotFound(role_id=command.role_id))
    return Ok(RoleDeleted(occurred_at=now, role_id=role.id, name=role.name.value, tenant_id=role.tenant_id))


def map_permission(
    role: Role | None,
    permission: Permission | None,
    command: MapPermission,
    *,
    now: datetime,
) -> Result[PermissionMappedToRole, AccessError]:
    if role is None:
        return Error(RoleNotFound(role_id=command.role_id))
    if permission is None:
        return Error(PermissionNotFound(permission_id=command.permission_id))
    return Ok(_mapped(role, permission, now))


def map_named_permissions(
    role: Role,
    known: Sequence[Permission],
    command: MapNamedPermissions,
    *,
    now: datetime,
) -> Result[tuple[PermissionMappedToRole, ...], AccessError]:
    """``known`` holds the permissions that exist among the command's names."""
    by_name = {permission.name.value: permission for permission in known}
    events = []
    for name in command.names:
        permission = by_name.get(name)
        if permission is None:
            return Error(PermissionNameNotFound(name=name))
        events.append(_mapped(role, permission, now))
    return Ok(tuple(events))


def _mapped(role: Role, permission: Permission, now: datetime) -> PermissionMappedToRole:
    return PermissionMappedToRole(
        occurred_at=now,
        role_id=role.id,
        permission_id=permission.id,
        permission_name=permission.name.value,
    )


def assign_role(
    role: Role | None,
    held: Assignment | None,
    command: AssignRole,
    *,
    now: datetime,
) -> Result[tuple[Assignment, RoleAssigned], AccessError]:
    """``held`` is the user's existing assignment of this role in this tenant.

    A global role may be assigned in any tenant, a tenant's role only in that tenant.
    """
    if role is None:
        return Error(RoleNotFound(role_id=command.role_id))
    if role.tenant_id is not None and role.tenant_id != command.tenant_id:
        return Error(RoleOutOfScope(role_id=role.id, tenant_id=command.tenant_id))
    if held is not None:
        return Error(RoleAlreadyAssigned(user_id=command.user_id, tenant_id=command.tenant_id, role_id=command.role_id))
    assignment = Assignment(
        user_id=command.user_id,
        tenant_id=command.tenant_id,
        role_id=role.id,
        assigned_by=command.assigned_by,
        assigned_at=now,
    )
    event = RoleAssigned(
        occurred_at=now,
        user_id=assignment.user_id,
        tenant_id=assignment.tenant_id,
        role_id=assignment.role_id,
        role_name=role.name.value,
        assigned_by=assignment.assigned_by,
    )
    return Ok((assignment, event))


def unassign_role(was_held: bool, command: UnassignRole, *, now: datetime) -> Result[RoleUnassigned, AccessError]:
    """``was_held`` says whether the user held the role in the tenant."""
    if not was_held:
        return Error(AssignmentNotFound(user_id=command.user_id, tenant_id=command.tenant_id, role_id=command.role_id))
    return Ok(
        RoleUnassigned(occurred_at=now, user_id=command.user_id, tenant_id=command.tenant_id, role_id=command.role_id)
    )


def create_permission(
    name_holder: Permission | None,
    command: CreatePermission,
    *,
    permission_id: uuid.UUID,
    now: datetime,
) -> Result[tuple[Permission, PermissionCreated], AccessError]:
    """``name_holder`` is the permission that already has the command's name."""
    if name_holder is not None:
        return Error(PermissionNameTaken(name=command.name.value))
    permission = Permission(
        id=permission_id,
        name=command.name,
        description=command.description,
        created_at=now,
        updated_at=now,
    )
    event = PermissionCreated(
        occurred_at=now,
        permission_id=permission.id,
        name=permission.name.value,
        description=permission.description,
    )
    return Ok((permission, event))


def update_permission(
    permission: Permission | None,
    name_holder: Permission | None,
    command: UpdatePermission,
    *,
    now: datetime,
) -> Result[tuple[Permission, PermissionUpdated], AccessError]:
    """``name_holder`` is the permission that has the command's new name."""
    if permission is None:
        return Error(PermissionNotFound(permission_id=command.permission_id))
    if command.name is not None and name_holder is not None and name_holder.id != permission.id:
        return Error(PermissionNameTaken(name=command.name.value))
    updated = replace(
        permission,
        name=permission.name if command.name is None else command.name,
        description=permission.description if command.description is None else command.description,
        updated_at=now,
    )
    event = PermissionUpdated(
        occurred_at=now,
        permission_id=updated.id,
        name=updated.name.value,
        description=updated.description,
    )
    return Ok((updated, event))


def delete_permission(
    permission: Permission | None,
    command: DeletePermission,
    *,
    now: datetime,
) -> Result[PermissionDeleted, AccessError]:
    if permission is None:
        return Error(PermissionNotFound(permission_id=command.permission_id))
    return Ok(PermissionDeleted(occurred_at=now, permission_id=permission.id, name=permission.name.value))


# The actions a caller may not take on itself: it could not sign back in to undo them.
_NOT_ON_SELF = frozenset({MemberAction.DEACTIVATE, MemberAction.REMOVE})


def act_on_member(member: TenantMember, command: ActOnMember) -> Result[MemberClearance, AccessError]:
    """Whether a tenant's ``owner`` or ``admin`` may deactivate, activate or remove ``member``.

    Its ``owner`` only by an owner, and its ``operator`` only by an operator
    (``OPERATOR_REQUIRED``, which the caller of this decides); never the caller
    itself for a deactivation or a removal. A user the tenant does not have holds
    no role here and is cleared, to be refused by the operation alike whether or
    not it exists.
    """
    if command.action in _NOT_ON_SELF and command.caller_id is not None and command.caller_id == member.user_id:
        return Error(ActingOnSelf(user_id=member.user_id))
    if OWNER_ROLE in member.roles and OWNER_ROLE not in command.caller_roles:
        return Error(OwnerRequired(user_id=member.user_id))
    if OPERATOR_ROLE in member.roles:
        return Ok(MemberClearance.OPERATOR_REQUIRED)
    return Ok(MemberClearance.CLEARED)
