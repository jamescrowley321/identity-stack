"""Dependency factories for identity domain services (onion architecture DI wiring).

AC-2.1.7: get_user_service() wires AsyncSession -> UserRepository -> UserService
AC-2.2.6: get_role_service(), get_permission_service(), get_tenant_service()
with the sync adapter of the configured provider (get_sync_adapter): DescopeSyncAdapter
wrapping the singleton DescopeManagementClient by default, KeycloakSyncAdapter with
SYNC_PROVIDER=keycloak, ZitadelSyncAdapter with SYNC_PROVIDER=zitadel.
AC-3.1.1: get_inbound_sync_service() wires repositories for inbound sync.
AC-3.2.1: get_reconciliation_service() wires repositories + Descope client for reconciliation.
AC-4.1.3: get_idp_link_service(), get_provider_service() for IdP link/provider domain services.
AC-4.3.1: get_identity_resolution_service() for identity resolution with Redis cache.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State

from app.dependencies.keycloak_sync import build_keycloak_sync_adapter
from app.dependencies.zitadel_sync import build_zitadel_sync_adapter
from app.errors.identity import Conflict, IdentityErrorRaised
from app.knobs import knob
from app.logbook import logbook
from app.models.database import get_async_session
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.sync_event import SyncEventRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.adapters.base import IdentityProviderAdapter
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.identity_provisioning import IdentityProvisioningService
from app.services.identity_resolution import IdentityResolutionService
from app.services.idp_link import IdPLinkService
from app.services.inbound_sync import InboundSyncService
from app.services.permission import PermissionService
from app.services.provider import ProviderService
from app.services.reconciliation import ReconciliationService
from app.services.role import RoleService
from app.services.sync_status import SyncStatusService
from app.services.tenant import TenantService
from app.services.user import UserService

# Postgres advisory lock ID for reconciliation (arbitrary constant)
_RECONCILIATION_LOCK_ID = 73_82_69_67  # "RECON" in digits

logger = logbook(__name__)

# The provider the canonical services push tenants, users and assignments to.
# Fixed for the process; main.lifespan refuses a value not in SYNC_ADAPTERS and
# opens what the chosen adapter needs (app.state.keycloak_admin for keycloak,
# app.state.zitadel_admin for zitadel).
SYNC_PROVIDER = knob("SYNC_PROVIDER", "descope").strip().lower()


# One entry per provider: how its adapter is built from the application state and
# the request's database session.
SYNC_ADAPTERS: dict[str, Callable[[State, AsyncSession], IdentityProviderAdapter]] = {
    "descope": lambda state, _session: DescopeSyncAdapter(client=state.descope_client),
    "keycloak": lambda state, session: build_keycloak_sync_adapter(state.keycloak_admin, session),
    "zitadel": lambda state, session: build_zitadel_sync_adapter(state.zitadel_admin, session),
}


# Reconciliation reads Descope and writes what it finds into the canonical model,
# so it is refused where the canonical model is pushed to another provider.
RECONCILIATION_NEEDS_DESCOPE = "Reconciliation compares the canonical model with Descope; SYNC_PROVIDER is not descope"


def get_sync_adapter(request: Request, session: AsyncSession) -> IdentityProviderAdapter:
    """The sync adapter of the configured provider (``SYNC_PROVIDER``)."""
    return SYNC_ADAPTERS[SYNC_PROVIDER](request.app.state, session)


async def get_user_service(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> UserService:
    """Build a UserService with its repository and sync adapter.

    Wiring: AsyncSession -> UserRepository(session) + UserTenantRoleRepository(session)
            get_sync_adapter(request, session)
            -> UserService(repository, adapter, assignment_repository)
    """
    repository = UserRepository(session)
    assignment_repository = UserTenantRoleRepository(session)
    adapter = get_sync_adapter(request, session)
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
            get_sync_adapter(request, session)
            -> RoleService(repository, permission_repository, assignment_repository, adapter)
    """
    repository = RoleRepository(session)
    permission_repository = PermissionRepository(session)
    assignment_repository = UserTenantRoleRepository(session)
    adapter = get_sync_adapter(request, session)
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
            get_sync_adapter(request, session)
            -> PermissionService(repository, adapter)
    """
    repository = PermissionRepository(session)
    adapter = get_sync_adapter(request, session)
    return PermissionService(repository=repository, adapter=adapter, publisher=request.app.state.cache_publisher)


async def get_tenant_service(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> TenantService:
    """Build a TenantService with its repository and sync adapter.

    Wiring: AsyncSession -> TenantRepository(session)
            get_sync_adapter(request, session)
            -> TenantService(repository, adapter)
    """
    repository = TenantRepository(session)
    adapter = get_sync_adapter(request, session)
    return TenantService(repository=repository, adapter=adapter, publisher=request.app.state.cache_publisher)


async def get_inbound_sync_service(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> InboundSyncService:
    """Build an InboundSyncService with its repositories.

    AC-3.1.1: Wiring: AsyncSession -> UserRepository + IdPLinkRepository + ProviderRepository + SyncEventRepository
               -> InboundSyncService(user_repository, idp_link_repository, provider_repository, sync_event_repository)
    """
    user_repository = UserRepository(session)
    idp_link_repository = IdPLinkRepository(session)
    provider_repository = ProviderRepository(session)
    sync_event_repository = SyncEventRepository(session)
    return InboundSyncService(
        user_repository=user_repository,
        idp_link_repository=idp_link_repository,
        provider_repository=provider_repository,
        sync_event_repository=sync_event_repository,
        publisher=request.app.state.cache_publisher,
    )


async def get_sync_status_service(
    session: AsyncSession = Depends(get_async_session),
) -> SyncStatusService:
    return SyncStatusService(
        provider_repository=ProviderRepository(session),
        idp_link_repository=IdPLinkRepository(session),
        sync_event_repository=SyncEventRepository(session),
    )


async def get_reconciliation_service(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> ReconciliationService:
    """Build a ReconciliationService with all repositories and Descope client.

    AC-3.2.1: Wiring: AsyncSession -> all identity repositories + DescopeManagementClient
               -> ReconciliationService(session, acquire_lock, descope_client, repositories...)
    Advisory lock callable injected here to keep SA imports out of the domain service.
    Refused (409) unless ``SYNC_PROVIDER`` is ``descope``.
    """
    if SYNC_PROVIDER != "descope":
        raise IdentityErrorRaised(Conflict(message=RECONCILIATION_NEEDS_DESCOPE))
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


async def get_identity_provisioning_service(
    session: AsyncSession = Depends(get_async_session),
) -> IdentityProvisioningService:
    """Build an IdentityProvisioningService (JIT provisioning write path).

    Wiring: AsyncSession -> Provider/IdPLink/User/Tenant/Role/UserTenantRole
             repositories -> IdentityProvisioningService.
    """
    return IdentityProvisioningService(
        session=session,
        provider_repository=ProviderRepository(session),
        idp_link_repository=IdPLinkRepository(session),
        user_repository=UserRepository(session),
        tenant_repository=TenantRepository(session),
        role_repository=RoleRepository(session),
        assignment_repository=UserTenantRoleRepository(session),
    )
