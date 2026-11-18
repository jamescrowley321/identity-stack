"""Entities and value objects of the Access context."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from expression import Error, Ok, Result

from app.domain.access.errors import InvalidName, NameFault, NameSubject

MAX_NAME_LENGTH = 255


@dataclass(frozen=True)
class RoleName:
    value: str


@dataclass(frozen=True)
class PermissionName:
    value: str


def _name_fault(raw: str) -> NameFault | None:
    if not raw:
        return NameFault.EMPTY
    if len(raw) > MAX_NAME_LENGTH:
        return NameFault.TOO_LONG
    return None


def role_name(raw: str) -> Result[RoleName, InvalidName]:
    """Build a RoleName for a new or renamed role."""
    fault = _name_fault(raw)
    return Ok(RoleName(raw)) if fault is None else Error(InvalidName(NameSubject.ROLE, fault))


def permission_name(raw: str) -> Result[PermissionName, InvalidName]:
    """Build a PermissionName for a new or renamed permission."""
    fault = _name_fault(raw)
    return Ok(PermissionName(raw)) if fault is None else Error(InvalidName(NameSubject.PERMISSION, fault))


@dataclass(frozen=True)
class Role:
    """A named set of permissions. ``tenant_id`` None means the role is global."""

    id: uuid.UUID
    name: RoleName
    description: str
    tenant_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Permission:
    id: uuid.UUID
    name: PermissionName
    description: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Assignment:
    """A role a user holds in one tenant."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role_id: uuid.UUID
    assigned_by: uuid.UUID | None
    assigned_at: datetime
