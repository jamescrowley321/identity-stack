"""Commands of the Access context: what a caller asks for."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.domain.access.values import PermissionName, RoleName


@dataclass(frozen=True)
class CreateRole:
    name: RoleName
    description: str
    tenant_id: uuid.UUID | None


@dataclass(frozen=True)
class UpdateRole:
    """A field left as None keeps its current value."""

    role_id: uuid.UUID
    name: RoleName | None
    description: str | None


@dataclass(frozen=True)
class DeleteRole:
    role_id: uuid.UUID


@dataclass(frozen=True)
class MapPermission:
    role_id: uuid.UUID
    permission_id: uuid.UUID


@dataclass(frozen=True)
class MapNamedPermissions:
    role_id: uuid.UUID
    names: tuple[str, ...]


@dataclass(frozen=True)
class AssignRole:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role_id: uuid.UUID
    assigned_by: uuid.UUID | None


@dataclass(frozen=True)
class UnassignRole:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role_id: uuid.UUID


@dataclass(frozen=True)
class CreatePermission:
    name: PermissionName
    description: str


@dataclass(frozen=True)
class UpdatePermission:
    """A field left as None keeps its current value."""

    permission_id: uuid.UUID
    name: PermissionName | None
    description: str | None


@dataclass(frozen=True)
class DeletePermission:
    permission_id: uuid.UUID
