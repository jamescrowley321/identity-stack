"""Dependency factories for identity domain services (onion architecture DI wiring).

AC-2.1.7: get_user_service() wires AsyncSession -> UserRepository -> UserService
AC-2.2.6: get_role_service(), get_permission_service(), get_tenant_service()
with DescopeSyncAdapter wrapping the singleton DescopeManagementClient.
AC-3.1.1: get_inbound_sync_service() wires repositories for inbound sync.
AC-3.2.1: get_reconciliation_service() wires repositories + Descope client for reconciliation.
"""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_async_session
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.descope import get_descope_client
from app.services.inbound_sync import InboundSyncService
from app.services.permission import PermissionService
from app.services.reconciliation import ReconciliationService
from app.services.role import RoleService
from app.services.tenant import TenantService
from app.services.user import UserService


async def get_user_service(
    session: AsyncSession = Depends(get_async_session),
) -> UserService:
    """Build a UserService with its repository and sync adapter.

    Wiring: AsyncSession -> UserRepository(session) + UserTenantRoleRepository(session)
            DescopeManagementClient -> DescopeSyncAdapter(client)
            -> UserService(repository, adapter, assignment_repository)
    """
    repository = UserRepository(session)
    assignment_repository = UserTenantRoleRepository(session)
    adapter = DescopeSyncAdapter(client=get_descope_client())
    return UserService(repository=repository, adapter=adapter, assignment_repository=assignment_repository)


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


async def get_inbound_sync_service(
    session: AsyncSession = Depends(get_async_session),
) -> InboundSyncService:
    """Build an InboundSyncService with its repositories.

    AC-3.1.1: Wiring: AsyncSession -> UserRepository + IdPLinkRepository + ProviderRepository
               -> InboundSyncService(user_repository, idp_link_repository, provider_repository)
    """
    user_repository = UserRepository(session)
    idp_link_repository = IdPLinkRepository(session)
    provider_repository = ProviderRepository(session)
    return InboundSyncService(
        user_repository=user_repository,
        idp_link_repository=idp_link_repository,
        provider_repository=provider_repository,
    )


async def get_reconciliation_service(
    session: AsyncSession = Depends(get_async_session),
) -> ReconciliationService:
    """Build a ReconciliationService with all repositories and Descope client.

    AC-3.2.1: Wiring: AsyncSession -> all identity repositories + DescopeManagementClient
               -> ReconciliationService(session, descope_client, repositories...)
    """
    user_repository = UserRepository(session)
    role_repository = RoleRepository(session)
    permission_repository = PermissionRepository(session)
    tenant_repository = TenantRepository(session)
    idp_link_repository = IdPLinkRepository(session)
    provider_repository = ProviderRepository(session)
    return ReconciliationService(
        session=session,
        descope_client=get_descope_client(),
        user_repository=user_repository,
        role_repository=role_repository,
        permission_repository=permission_repository,
        tenant_repository=tenant_repository,
        idp_link_repository=idp_link_repository,
        provider_repository=provider_repository,
    )
