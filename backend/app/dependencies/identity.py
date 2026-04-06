"""Dependency factories for identity domain services (onion architecture DI wiring).

AC-2.1.7: get_user_service() wires AsyncSession -> UserRepository -> UserService
AC-2.2.6: get_role_service(), get_permission_service(), get_tenant_service()
with DescopeSyncAdapter wrapping the singleton DescopeManagementClient.
"""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_async_session
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.descope import get_descope_client
from app.services.permission import PermissionService
from app.services.role import RoleService
from app.services.tenant import TenantService
from app.services.user import UserService


async def get_user_service(
    session: AsyncSession = Depends(get_async_session),
) -> UserService:
    """Build a UserService with its repository and sync adapter.

    Wiring: AsyncSession -> UserRepository(session)
            DescopeManagementClient -> DescopeSyncAdapter(client)
            -> UserService(repository, adapter)
    """
    repository = UserRepository(session)
    adapter = DescopeSyncAdapter(client=get_descope_client())
    return UserService(repository=repository, adapter=adapter)


async def get_role_service(
    session: AsyncSession = Depends(get_async_session),
) -> RoleService:
    """Build a RoleService with its repositories and sync adapter.

    Wiring: AsyncSession -> RoleRepository + PermissionRepository + UserTenantRoleRepository
            DescopeManagementClient -> DescopeSyncAdapter(client)
            -> RoleService(repository, permission_repository, assignment_repository, adapter)
    """
    repository = RoleRepository(session)
    permission_repository = PermissionRepository(session)
    assignment_repository = UserTenantRoleRepository(session)
    adapter = DescopeSyncAdapter(client=get_descope_client())
    return RoleService(
        repository=repository,
        permission_repository=permission_repository,
        assignment_repository=assignment_repository,
        adapter=adapter,
    )


async def get_permission_service(
    session: AsyncSession = Depends(get_async_session),
) -> PermissionService:
    """Build a PermissionService with its repository and sync adapter.

    Wiring: AsyncSession -> PermissionRepository(session)
            DescopeManagementClient -> DescopeSyncAdapter(client)
            -> PermissionService(repository, adapter)
    """
    repository = PermissionRepository(session)
    adapter = DescopeSyncAdapter(client=get_descope_client())
    return PermissionService(repository=repository, adapter=adapter)


async def get_tenant_service(
    session: AsyncSession = Depends(get_async_session),
) -> TenantService:
    """Build a TenantService with its repository and sync adapter.

    Wiring: AsyncSession -> TenantRepository(session)
            DescopeManagementClient -> DescopeSyncAdapter(client)
            -> TenantService(repository, adapter)
    """
    repository = TenantRepository(session)
    adapter = DescopeSyncAdapter(client=get_descope_client())
    return TenantService(repository=repository, adapter=adapter)
