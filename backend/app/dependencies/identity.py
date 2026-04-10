"""Dependency factories for identity domain services (onion architecture DI wiring).

AC-2.1.7: get_user_service() wires AsyncSession -> UserRepository -> UserService
AC-2.2.6: get_role_service(), get_permission_service(), get_tenant_service()
with DescopeSyncAdapter wrapping the singleton DescopeManagementClient.
AC-3.1.1: get_inbound_sync_service() wires repositories for inbound sync.
AC-3.2.1: get_reconciliation_service() wires repositories + Descope client for reconciliation.
AC-4.1.3: get_idp_link_service(), get_provider_service() for IdP link/provider domain services.
AC-4.3.1: get_identity_resolution_service() for identity resolution with Redis cache.
"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy import text
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
from app.services.identity_resolution import IdentityResolutionService
from app.services.idp_link import IdPLinkService
from app.services.inbound_sync import InboundSyncService
from app.services.permission import PermissionService
from app.services.provider import ProviderService
from app.services.reconciliation import ReconciliationService
from app.services.role import RoleService
from app.services.tenant import TenantService
from app.services.user import UserService

# Postgres advisory lock ID for reconciliation (arbitrary constant)
_RECONCILIATION_LOCK_ID = 73_82_69_67  # "RECON" in digits


async def get_user_service(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> UserService:
    """Build a UserService with its repository and sync adapter.

    Wiring: AsyncSession -> UserRepository(session) + UserTenantRoleRepository(session)
            DescopeManagementClient -> DescopeSyncAdapter(client)
            -> UserService(repository, adapter, assignment_repository)
    """
    repository = UserRepository(session)
    assignment_repository = UserTenantRoleRepository(session)
    adapter = DescopeSyncAdapter(client=request.app.state.descope_client)
    return UserService(
        repository=repository,
        adapter=adapter,
        assignment_repository=assignment_repository,
        publisher=request.app.state.cache_publisher,
    )


async def get_role_service(
    request: Request,
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
    adapter = DescopeSyncAdapter(client=request.app.state.descope_client)
    return RoleService(
        repository=repository,
        permission_repository=permission_repository,
        assignment_repository=assignment_repository,
        adapter=adapter,
        publisher=request.app.state.cache_publisher,
    )


async def get_permission_service(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> PermissionService:
    """Build a PermissionService with its repository and sync adapter.

    Wiring: AsyncSession -> PermissionRepository(session)
            DescopeManagementClient -> DescopeSyncAdapter(client)
            -> PermissionService(repository, adapter)
    """
    repository = PermissionRepository(session)
    adapter = DescopeSyncAdapter(client=request.app.state.descope_client)
    return PermissionService(repository=repository, adapter=adapter, publisher=request.app.state.cache_publisher)


async def get_tenant_service(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> TenantService:
    """Build a TenantService with its repository and sync adapter.

    Wiring: AsyncSession -> TenantRepository(session)
            DescopeManagementClient -> DescopeSyncAdapter(client)
            -> TenantService(repository, adapter)
    """
    repository = TenantRepository(session)
    adapter = DescopeSyncAdapter(client=request.app.state.descope_client)
    return TenantService(repository=repository, adapter=adapter, publisher=request.app.state.cache_publisher)


async def get_inbound_sync_service(
    request: Request,
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
        publisher=request.app.state.cache_publisher,
    )


async def get_reconciliation_service(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> ReconciliationService:
    """Build a ReconciliationService with all repositories and Descope client.

    AC-3.2.1: Wiring: AsyncSession -> all identity repositories + DescopeManagementClient
               -> ReconciliationService(session, acquire_lock, descope_client, repositories...)
    Advisory lock callable injected here to keep SA imports out of the domain service.
    """
    user_repository = UserRepository(session)
    role_repository = RoleRepository(session)
    permission_repository = PermissionRepository(session)
    tenant_repository = TenantRepository(session)
    idp_link_repository = IdPLinkRepository(session)
    provider_repository = ProviderRepository(session)

    async def _acquire_lock() -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _RECONCILIATION_LOCK_ID},
        )

    return ReconciliationService(
        session=session,
        acquire_lock=_acquire_lock,
        descope_client=request.app.state.descope_client,
        user_repository=user_repository,
        role_repository=role_repository,
        permission_repository=permission_repository,
        tenant_repository=tenant_repository,
        idp_link_repository=idp_link_repository,
        provider_repository=provider_repository,
        publisher=request.app.state.cache_publisher,
    )


async def get_idp_link_service(
    session: AsyncSession = Depends(get_async_session),
) -> IdPLinkService:
    """Build an IdPLinkService with its repositories.

    AC-4.1.3: Wiring: AsyncSession -> IdPLinkRepository + UserRepository + ProviderRepository
               -> IdPLinkService(repository, user_repository, provider_repository)
    """
    repository = IdPLinkRepository(session)
    user_repository = UserRepository(session)
    provider_repository = ProviderRepository(session)
    return IdPLinkService(
        repository=repository,
        user_repository=user_repository,
        provider_repository=provider_repository,
    )


async def get_provider_service(
    session: AsyncSession = Depends(get_async_session),
) -> ProviderService:
    """Build a ProviderService with its repository.

    AC-4.1.3: Wiring: AsyncSession -> ProviderRepository(session)
               -> ProviderService(repository)
    """
    repository = ProviderRepository(session)
    return ProviderService(repository=repository)


async def get_identity_resolution_service(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> IdentityResolutionService:
    """Build an IdentityResolutionService with its repositories and Redis client.

    AC-4.3.1: Wiring: AsyncSession -> UserRepository + IdPLinkRepository + ProviderRepository
               + UserTenantRoleRepository + RoleRepository + TenantRepository
               + optional Redis client -> IdentityResolutionService
    """
    user_repository = UserRepository(session)
    idp_link_repository = IdPLinkRepository(session)
    provider_repository = ProviderRepository(session)
    assignment_repository = UserTenantRoleRepository(session)
    role_repository = RoleRepository(session)
    tenant_repository = TenantRepository(session)
    return IdentityResolutionService(
        user_repository=user_repository,
        idp_link_repository=idp_link_repository,
        provider_repository=provider_repository,
        assignment_repository=assignment_repository,
        role_repository=role_repository,
        tenant_repository=tenant_repository,
        redis_client=request.app.state.redis_client,
    )
