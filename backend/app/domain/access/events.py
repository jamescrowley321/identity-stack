"""Domain events of the Access context: what a decision says has happened."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, kw_only=True)
class AccessEvent:
    """Base of every Access event."""

    occurred_at: datetime


@dataclass(frozen=True, kw_only=True)
class RoleCreated(AccessEvent):
    role_id: uuid.UUID
    name: str
    description: str
    tenant_id: uuid.UUID | None


@dataclass(frozen=True, kw_only=True)
class RoleUpdated(AccessEvent):
    role_id: uuid.UUID
    name: str
    description: str
    tenant_id: uuid.UUID | None


@dataclass(frozen=True, kw_only=True)
class RoleDeleted(AccessEvent):
    role_id: uuid.UUID
    name: str
    tenant_id: uuid.UUID | None


@dataclass(frozen=True, kw_only=True)
class PermissionMappedToRole(AccessEvent):
    role_id: uuid.UUID
    permission_id: uuid.UUID
    permission_name: str


@dataclass(frozen=True, kw_only=True)
class RoleAssigned(AccessEvent):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role_id: uuid.UUID
    role_name: str
    assigned_by: uuid.UUID | None


@dataclass(frozen=True, kw_only=True)
class RoleUnassigned(AccessEvent):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role_id: uuid.UUID


@dataclass(frozen=True, kw_only=True)
class PermissionCreated(AccessEvent):
    permission_id: uuid.UUID
    name: str
    description: str


@dataclass(frozen=True, kw_only=True)
class PermissionUpdated(AccessEvent):
    permission_id: uuid.UUID
    name: str
    description: str


@dataclass(frozen=True, kw_only=True)
class PermissionDeleted(AccessEvent):
    permission_id: uuid.UUID
    name: str
