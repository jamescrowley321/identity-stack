"""Translate Access domain errors into the IdentityError a service returns."""

from __future__ import annotations

from app.domain import access
from app.errors.identity import Conflict, IdentityError, NotFound, ValidationError

_NAME_SUBJECTS = {
    access.NameSubject.ROLE: "Role",
    access.NameSubject.PERMISSION: "Permission",
}
_NAME_FAULTS = {
    access.NameFault.EMPTY: "must not be empty",
    access.NameFault.TOO_LONG: f"must be at most {access.MAX_NAME_LENGTH} characters",
}


def identity_error(error: access.AccessError) -> IdentityError:
    match error:
        case access.RoleNameTaken(name=name):
            return Conflict(message=f"Role '{name}' already exists in this scope")
        case access.RoleNotFound(role_id=role_id):
            return NotFound(message=f"Role '{role_id}' not found")
        case access.RoleOutOfScope(role_id=role_id):
            # Answered as not found, so a caller cannot probe another tenant's roles.
            return NotFound(message=f"Role '{role_id}' not found")
        case access.PermissionNameTaken(name=name):
            return Conflict(message=f"Permission '{name}' already exists")
        case access.PermissionNotFound(permission_id=permission_id):
            return NotFound(message=f"Permission '{permission_id}' not found")
        case access.PermissionNameNotFound(name=name):
            return NotFound(message=f"Permission '{name}' not found")
        case access.PermissionAlreadyMapped(role_id=role_id, permission_id=permission_id):
            return Conflict(message=f"Permission '{permission_id}' is already mapped to role '{role_id}'")
        case access.RoleAlreadyAssigned(user_id=user_id, tenant_id=tenant_id, role_id=role_id):
            return Conflict(message=f"User '{user_id}' already has role '{role_id}' in tenant '{tenant_id}'")
        case access.AssignmentNotFound(user_id=user_id, tenant_id=tenant_id):
            return NotFound(message=f"Role assignment not found for user '{user_id}' in tenant '{tenant_id}'")
        case access.InvalidName(subject=subject, fault=fault):
            return ValidationError(message=f"{_NAME_SUBJECTS[subject]} name {_NAME_FAULTS[fault]}")
    raise TypeError(f"no IdentityError for {type(error).__name__}")
