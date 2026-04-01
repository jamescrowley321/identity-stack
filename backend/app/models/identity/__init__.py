"""Canonical identity domain models."""

from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider
from app.models.identity.role import Permission, Role, RolePermission
from app.models.identity.tenant import Tenant
from app.models.identity.user import IdPLink, User

__all__ = [
    "IdPLink",
    "Permission",
    "Provider",
    "Role",
    "RolePermission",
    "Tenant",
    "User",
    "UserTenantRole",
]
