"""Expected failures of the Access context, as values."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum


class NameSubject(Enum):
    ROLE = "role"
    PERMISSION = "permission"


class NameFault(Enum):
    EMPTY = "empty"
    TOO_LONG = "too_long"


@dataclass(frozen=True)
class AccessError:
    """Base of every expected failure in the Access context."""


@dataclass(frozen=True)
class InvalidName(AccessError):
    subject: NameSubject
    fault: NameFault


@dataclass(frozen=True)
class RoleNameTaken(AccessError):
    """Another role in the same scope already has this name."""

    name: str


@dataclass(frozen=True)
class RoleNotFound(AccessError):
    role_id: uuid.UUID


@dataclass(frozen=True)
class RoleOutOfScope(AccessError):
    """The role belongs to a tenant other than the one it was asked for in."""

    role_id: uuid.UUID
    tenant_id: uuid.UUID


@dataclass(frozen=True)
class PermissionNameTaken(AccessError):
    name: str


@dataclass(frozen=True)
class PermissionNotFound(AccessError):
    permission_id: uuid.UUID


@dataclass(frozen=True)
class PermissionNameNotFound(AccessError):
    name: str


@dataclass(frozen=True)
class PermissionAlreadyMapped(AccessError):
    role_id: uuid.UUID
    permission_id: uuid.UUID


@dataclass(frozen=True)
class RoleAlreadyAssigned(AccessError):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role_id: uuid.UUID


@dataclass(frozen=True)
class AssignmentNotFound(AccessError):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role_id: uuid.UUID


@dataclass(frozen=True)
class NotATenantMember(AccessError):
    """The user holds no role in the tenant: a role is assigned only to a member.

    An unknown user and a user of another tenant are the same failure, so the
    answer tells a caller nothing about users outside its tenant.
    """

    user_id: uuid.UUID
    tenant_id: uuid.UUID


@dataclass(frozen=True)
class ActingOnSelf(AccessError):
    """A caller deactivates or removes itself: it could not sign back in to undo it."""

    user_id: str


@dataclass(frozen=True)
class OwnerRequired(AccessError):
    """The member holds ``owner`` in the tenant: only an owner deactivates, activates or removes it."""

    user_id: str
