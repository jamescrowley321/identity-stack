"""Rows of the Access tables map to domain values and back without loss."""

import uuid
from datetime import datetime, timezone

from app.domain import access
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Permission, Role
from app.repositories.assignment import assignment_from_row, assignment_to_row
from app.repositories.permission import apply_permission, permission_from_row, permission_to_row
from app.repositories.role import apply_role, role_from_row, role_to_row

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
UPDATED = datetime(2026, 2, 1, tzinfo=timezone.utc)
TENANT_ID = uuid.uuid4()


def _role() -> access.Role:
    return access.Role(
        id=uuid.uuid4(),
        name=access.RoleName("editor"),
        description="Edits",
        tenant_id=TENANT_ID,
        created_at=CREATED,
        updated_at=UPDATED,
    )


def _permission() -> access.Permission:
    return access.Permission(
        id=uuid.uuid4(),
        name=access.PermissionName("documents.read"),
        description="Read",
        created_at=CREATED,
        updated_at=UPDATED,
    )


def test_role_round_trips():
    role = _role()

    row = role_to_row(role)

    assert isinstance(row, Role)
    assert row.model_dump() == {
        "id": role.id,
        "name": "editor",
        "description": "Edits",
        "tenant_id": TENANT_ID,
        "created_at": CREATED,
        "updated_at": UPDATED,
    }
    assert role_from_row(row) == role


def test_role_from_row_accepts_a_stored_name_no_new_role_could_take():
    row = Role(id=uuid.uuid4(), name="", description="", tenant_id=None, created_at=CREATED, updated_at=UPDATED)

    assert role_from_row(row).name == access.RoleName("")


def test_apply_role_writes_name_and_description_only():
    row = role_to_row(_role())
    changed = access.Role(
        id=uuid.uuid4(),
        name=access.RoleName("senior-editor"),
        description="Senior",
        tenant_id=None,
        created_at=UPDATED,
        updated_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    before = row.model_dump()

    apply_role(changed, row)

    assert row.model_dump() == before | {"name": "senior-editor", "description": "Senior"}


def test_permission_round_trips():
    permission = _permission()

    row = permission_to_row(permission)

    assert isinstance(row, Permission)
    assert row.model_dump() == {
        "id": permission.id,
        "name": "documents.read",
        "description": "Read",
        "created_at": CREATED,
        "updated_at": UPDATED,
    }
    assert permission_from_row(row) == permission


def test_apply_permission_writes_name_and_description_only():
    row = permission_to_row(_permission())
    changed = access.Permission(
        id=uuid.uuid4(),
        name=access.PermissionName("documents.view"),
        description="View",
        created_at=UPDATED,
        updated_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    before = row.model_dump()

    apply_permission(changed, row)

    assert row.model_dump() == before | {"name": "documents.view", "description": "View"}


def test_assignment_round_trips():
    assignment = access.Assignment(
        user_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        role_id=uuid.uuid4(),
        assigned_by=uuid.uuid4(),
        assigned_at=CREATED,
    )

    row = assignment_to_row(assignment)

    assert isinstance(row, UserTenantRole)
    assert (row.user_id, row.tenant_id, row.role_id, row.assigned_by, row.assigned_at) == (
        assignment.user_id,
        assignment.tenant_id,
        assignment.role_id,
        assignment.assigned_by,
        CREATED,
    )
    assert assignment_from_row(row) == assignment
