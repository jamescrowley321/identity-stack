"""Every Access domain error has one IdentityError, with the message callers already see."""

import uuid

import pytest

from app.domain import access
from app.errors.access import identity_error
from app.errors.identity import Conflict, NotFound, ValidationError

ROLE_ID = uuid.uuid4()
PERMISSION_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
TENANT_ID = uuid.uuid4()

TRANSLATIONS = [
    (access.RoleNameTaken(name="editor"), Conflict(message="Role 'editor' already exists in this scope")),
    (access.RoleNotFound(role_id=ROLE_ID), NotFound(message=f"Role '{ROLE_ID}' not found")),
    (
        access.RoleOutOfScope(role_id=ROLE_ID, tenant_id=TENANT_ID),
        NotFound(message=f"Role '{ROLE_ID}' not found"),
    ),
    (access.PermissionNameTaken(name="documents.read"), Conflict(message="Permission 'documents.read' already exists")),
    (
        access.PermissionNotFound(permission_id=PERMISSION_ID),
        NotFound(message=f"Permission '{PERMISSION_ID}' not found"),
    ),
    (access.PermissionNameNotFound(name="documents.read"), NotFound(message="Permission 'documents.read' not found")),
    (
        access.PermissionAlreadyMapped(role_id=ROLE_ID, permission_id=PERMISSION_ID),
        Conflict(message=f"Permission '{PERMISSION_ID}' is already mapped to role '{ROLE_ID}'"),
    ),
    (
        access.RoleAlreadyAssigned(user_id=USER_ID, tenant_id=TENANT_ID, role_id=ROLE_ID),
        Conflict(message=f"User '{USER_ID}' already has role '{ROLE_ID}' in tenant '{TENANT_ID}'"),
    ),
    (
        access.AssignmentNotFound(user_id=USER_ID, tenant_id=TENANT_ID, role_id=ROLE_ID),
        NotFound(message=f"Role assignment not found for user '{USER_ID}' in tenant '{TENANT_ID}'"),
    ),
    (
        access.InvalidName(access.NameSubject.ROLE, access.NameFault.EMPTY),
        ValidationError(message="Role name must not be empty"),
    ),
    (
        access.InvalidName(access.NameSubject.ROLE, access.NameFault.TOO_LONG),
        ValidationError(message="Role name must be at most 255 characters"),
    ),
    (
        access.InvalidName(access.NameSubject.PERMISSION, access.NameFault.EMPTY),
        ValidationError(message="Permission name must not be empty"),
    ),
    (
        access.InvalidName(access.NameSubject.PERMISSION, access.NameFault.TOO_LONG),
        ValidationError(message="Permission name must be at most 255 characters"),
    ),
]


@pytest.mark.parametrize(("error", "expected"), TRANSLATIONS, ids=lambda value: type(value).__name__)
def test_identity_error(error, expected):
    assert identity_error(error) == expected


def test_every_access_error_is_translated():
    translated = {type(error) for error, _ in TRANSLATIONS}
    assert translated == set(access.AccessError.__subclasses__())


def test_an_unknown_access_error_is_refused():
    with pytest.raises(TypeError):
        identity_error(access.AccessError())
