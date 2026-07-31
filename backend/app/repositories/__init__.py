"""Repository layer — data access only, no business logic."""

from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.base import BaseRepository, RepositoryConflictError
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.sync_event import SyncEventRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository

__all__ = [
    "BaseRepository",
    "IdPLinkRepository",
    "PermissionRepository",
    "ProviderRepository",
    "RepositoryConflictError",
    "RoleRepository",
    "SyncEventRepository",
    "TenantRepository",
    "UserRepository",
    "UserTenantRoleRepository",
]
