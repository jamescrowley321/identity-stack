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

import math
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction
from starlette.datastructures import State

from app.dependencies.zitadel_sync import build_zitadel_sync_adapter
from app.errors.identity import Conflict, IdentityErrorRaised
from app.knobs import knob
from app.logbook import logbook
from app.middleware.providers import ProviderConfigurationError
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
from app.services.adapters.keycloak import PROVIDER_NAME as KEYCLOAK_PROVIDER_NAME
from app.services.adapters.keycloak import (
    KeycloakSyncAdapter,
    LinkedSubject,
    LocalDatabaseError,
    OrganizationLock,
)
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


def keycloak_linked_subject(session: AsyncSession) -> LinkedSubject:
    """The Keycloak account id a canonical user's ``keycloak`` IdP link names, read in the request's session.

    Read in a savepoint of the request's transaction. A database failure is
    ``LocalDatabaseError``, after the savepoint is rolled back: the request's
    transaction stays usable and the rows it loaded stay loaded (a rollback of
    the whole session would expire them, and the service reads them after the
    push).
    """
    links = IdPLinkRepository(session)

    async def linked_subject(user_id: uuid.UUID) -> str | None:
        try:
            async with session.begin_nested():
                return await links.signed_in_sub_of(user_id, KEYCLOAK_PROVIDER_NAME)
        except _DATABASE_FAILURES as exc:
            raise LocalDatabaseError(_failed("the IdP link read", exc)) from exc

    return linked_subject


# What a database call raises when the database or the pool fails: SQLAlchemy's
# errors (the driver's wrapped, a pool checkout timeout, a lock not taken in
# time) and a refused connection.
_DATABASE_FAILURES = (SQLAlchemyError, OSError)


def _failed(what: str, exc: BaseException) -> str:
    """A LocalDatabaseError's message: the exception's type only, never the statement or its parameters."""
    return f"{what} failed in the service's database: {type(exc).__name__}"


# Namespaces the advisory-lock key of a Keycloak organization (hashed with its alias).
_ORGANIZATION_LOCK_NAMESPACE = "keycloak-organization:"


# Postgres reads lock_timeout as whole milliseconds: 0 is no limit, and above
# INT_MAX set_config refuses the value.
_LOCK_TIMEOUT_MS_BOUNDS = (1, 2_147_483_647)


def organization_lock_timeout() -> int:
    """``KEYCLOAK_ORGANIZATION_LOCK_TIMEOUT_SECONDS`` in whole milliseconds: a push's wait for an organization's lock.

    The default, 20 s, ends before SQLAlchemy's 30 s pool checkout timeout. It
    does not bound how long a holder keeps the lock (a holder's admin calls can
    fetch a token and retry after a 401, and httpx's timeout applies per phase),
    so a push behind a holder slower than the limit gives up and fails.

    Raises ``ProviderConfigurationError`` for a value that is not a number of
    seconds Postgres takes as a limit, 0.001 to 2147483.647 once rounded to the
    millisecond: a limit that cannot be read is not replaced by a guess.
    """
    raw = knob("KEYCLOAK_ORGANIZATION_LOCK_TIMEOUT_SECONDS", "20")
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    lowest, highest = _LOCK_TIMEOUT_MS_BOUNDS
    # Bounded in seconds before scaling: a finite value such as 1e306 would scale to inf.
    in_range = math.isfinite(seconds) and abs(seconds) <= highest
    milliseconds = round(seconds * 1000) if in_range else None
    if milliseconds is None or not lowest <= milliseconds <= highest:
        raise ProviderConfigurationError(
            "KEYCLOAK_ORGANIZATION_LOCK_TIMEOUT_SECONDS must be a number of seconds"
            f" from 0.001 to 2147483.647, not {raw!r}"
        )
    return milliseconds


# Fixed when this module is imported, whatever SYNC_PROVIDER is, so a value
# that is no limit stops startup.
ORGANIZATION_LOCK_TIMEOUT_MS = organization_lock_timeout()


def keycloak_organization_lock(session: AsyncSession) -> OrganizationLock:
    """A Postgres advisory lock per Keycloak organization alias, held across workers.

    Taken on the request's session connection, which the request already holds
    (the IdP link read), so taking it never waits for a second pooled connection.
    It is a transaction lock taken in a savepoint of the request's transaction
    and released by rolling that savepoint back, whether the body succeeded or
    raised: rolling back to a savepoint releases the locks taken after it, and
    expires no row the request loaded (the savepoint changed none). The adapter
    runs after the service committed, so the savepoint holds only the lock.

    A push waits at most ``ORGANIZATION_LOCK_TIMEOUT_MS`` for the lock
    (``lock_timeout``, local to the savepoint): requests waiting behind a slow
    holder each hold their session's connection, and the limit lets them go
    before they hold the pool long enough to stall other routes. A wait that
    runs out, or any database failure taking the lock, is ``LocalDatabaseError``.

    A failure releasing it is logged and does not change the push's outcome:
    the body's work in Keycloak was done (or its error is kept), and the lock
    ends with the request's transaction at the latest.
    """

    @asynccontextmanager
    async def organization_lock(alias: str) -> AsyncIterator[None]:
        try:
            savepoint = await session.begin_nested()
        except _DATABASE_FAILURES as exc:
            raise LocalDatabaseError(_failed("the organization lock", exc)) from exc
        try:
            await session.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": f"{ORGANIZATION_LOCK_TIMEOUT_MS}ms"},
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": _ORGANIZATION_LOCK_NAMESPACE + alias},
            )
        except _DATABASE_FAILURES as exc:
            await _release(savepoint, alias)
            raise LocalDatabaseError(_failed("the organization lock", exc)) from exc
        except BaseException:
            # A cancellation (a client disconnect, an outer timeout) mid-wait:
            # leave no savepoint holding lock_timeout or the lock.
            await _release(savepoint, alias)
            raise
        try:
            yield
        finally:
            await _release(savepoint, alias)

    return organization_lock


async def _release(savepoint: AsyncSessionTransaction, alias: str) -> None:
    """Roll the lock's savepoint back, which releases the lock; a failure is logged, never raised."""
    try:
        await savepoint.rollback()
    except _DATABASE_FAILURES as exc:
        logger.warning(
            "Keycloak organization lock for %s not released before the request's transaction ends: %s",
            alias,
            type(exc).__name__,
        )


# One entry per provider: how its adapter is built from the application state and
# the request's database session.
SYNC_ADAPTERS: dict[str, Callable[[State, AsyncSession], IdentityProviderAdapter]] = {
    "descope": lambda state, _session: DescopeSyncAdapter(client=state.descope_client),
    "keycloak": lambda state, session: KeycloakSyncAdapter(
        state.keycloak_admin,
        linked_subject=keycloak_linked_subject(session),
        organization_lock=keycloak_organization_lock(session),
    ),
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
