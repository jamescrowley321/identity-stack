"""Dependency factories for identity domain services (onion architecture DI wiring).

AC-2.1.7: get_user_service() wires AsyncSession -> UserRepository -> UserService
AC-2.2.6: get_role_service(), get_permission_service(), get_tenant_service()
with the sync adapter of the configured provider (get_sync_adapter): DescopeSyncAdapter
wrapping the singleton DescopeManagementClient by default, KeycloakSyncAdapter with
SYNC_PROVIDER=keycloak, ZitadelSyncAdapter with SYNC_PROVIDER=zitadel.
AC-3.1.1: get_inbound_sync_service() wires repositories for inbound sync.
AC-3.2.1: get_reconciliation_service() wires the reconciliation pass of the configured provider
(Descope's on the Descope client, Keycloak's on the admin client).
AC-4.1.3: get_idp_link_service(), get_provider_service() for IdP link/provider domain services.
AC-4.3.1: get_identity_resolution_service() for identity resolution with Redis cache.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from starlette.datastructures import State

from app.dependencies.descope import (
    get_descope_management,
    is_descope_principal,
    require_descope_management,
    require_descope_principal,
)
from app.dependencies.keycloak_reconciliation import build_keycloak_reconciliation
from app.dependencies.keycloak_sync import build_keycloak_sync_adapter
from app.dependencies.rbac import get_rbac_session, is_canonical_principal
from app.dependencies.zitadel_sync import build_zitadel_sync_adapter
from app.errors.identity import CapabilityMissing, Conflict, IdentityErrorRaised
from app.knobs import knob
from app.logbook import logbook
from app.middleware.providers import KEYCLOAK_PROVIDER_NAME, ZITADEL_PROVIDER_NAME, ProviderTokenConfig
from app.models.database import get_async_session, get_engine
from app.outbound import outbound_client
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.machine_credential import MachineCredentialRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.registration_tokens import registration_token_cipher
from app.repositories.role import RoleRepository
from app.repositories.sync_event import SyncEventRepository
from app.repositories.sync_ledger import PendingPushRepository, ReconciliationRunRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.adapters.base import Capability, IdentityProviderAdapter
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.adapters.keycloak import KeycloakSyncAdapter
from app.services.adapters.zitadel import ZitadelSyncAdapter
from app.services.identity_provisioning import IdentityProvisioningService
from app.services.identity_resolution import IdentityResolutionService
from app.services.idp_link import IdPLinkService
from app.services.inbound_sync import InboundSyncService
from app.services.machine_credentials import (
    CanonicalMachineCredentials,
    DescopeAccessKeys,
    MachineCredentials,
    MachineCredentialService,
)
from app.services.members import MemberDirectory, MemberService
from app.services.openfga import OPENFGA_TIMEOUT_SECONDS, OpenFgaRelationshipStore, connect_openfga
from app.services.permission import PermissionService
from app.services.provider import ProviderService
from app.services.reconciliation import ProviderReconciliation, ReconciliationService
from app.services.relationship_store import (
    DescopeRelationshipStore,
    RelationshipStore,
    RelationshipStoreFault,
    RelationshipStoreKind,
    RelationshipStoreNotAvailable,
)
from app.services.relationship_users import CanonicalUserDirectory, DescopeUserDirectory, UserDirectory
from app.services.role import RoleService
from app.services.sync_status import SyncStatusService
from app.services.tenant import TenantService
from app.services.user import UserService

# Postgres advisory lock ID for reconciliation (arbitrary constant)
_RECONCILIATION_LOCK_ID = 73_82_69_67  # "RECON" in digits
# ... and the session lock a pass that commits more than once holds from start to end.
_RECONCILIATION_PASS_LOCK_ID = 73_82_69_67_80  # "RECONP"
# ... and the session lock held while pending pushes are pushed: by a pass, or by the push retry.
_PENDING_PUSH_LOCK_ID = 80_85_83_72  # "PUSH"


_TRY_LOCK = text("SELECT pg_try_advisory_lock(:lock_id)")
_UNLOCK = text("SELECT pg_advisory_unlock(:lock_id)")
# How long a pass waits for a push retry turn in progress before it answers 409.
PASS_WAITS_FOR_PUSH_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.1


async def _take_lock(connection: AsyncConnection, lock_id: int, wait_seconds: float) -> bool:
    """Try the session lock until it is taken or ``wait_seconds`` have passed (``0``: once)."""
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        taken = bool((await connection.execute(_TRY_LOCK, {"lock_id": lock_id})).scalar())
        await connection.commit()
        if taken:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(_LOCK_POLL_SECONDS)


@asynccontextmanager
async def _session_locks(take: list[tuple[int, float]]) -> AsyncIterator[bool]:
    """Take each ``(lock id, seconds to wait)`` in turn on a connection of its own; ``False`` when one is not taken.

    Session-level locks survive the holder's commits. The connection is dropped,
    not returned to the pool, when taking or releasing a lock fails or is
    cancelled: closing it frees every lock its session holds, so none outlives
    its holder.
    """
    async with get_engine().connect() as connection:
        held: list[int] = []
        try:
            for lock_id, wait_seconds in take:
                if not await _take_lock(connection, lock_id, wait_seconds):
                    break
                held.append(lock_id)
        except BaseException:
            await connection.invalidate()
            raise
        try:
            yield len(held) == len(take)
        finally:
            try:
                for lock_id in reversed(held):
                    await connection.execute(_UNLOCK, {"lock_id": lock_id})
                await connection.commit()
            except BaseException as exc:
                logger.warning("Reconciliation: a session lock could not be released; dropping its connection")
                await connection.invalidate()
                if not isinstance(exc, Exception):
                    raise


def hold_reconciliation_pass() -> AbstractAsyncContextManager[bool]:
    """Hold the pass lock for the whole pass, then the pending-push lock; ``False`` when either is not taken.

    ``False`` at once while another pass runs, and after ``PASS_WAITS_FOR_PUSH_SECONDS``
    while a push retry turn still pushes.
    """
    return _session_locks([(_RECONCILIATION_PASS_LOCK_ID, 0.0), (_PENDING_PUSH_LOCK_ID, PASS_WAITS_FOR_PUSH_SECONDS)])


def hold_push_retry() -> AbstractAsyncContextManager[bool]:
    """Hold the pending-push lock for one push retry; ``False`` while a pass or another retry pushes."""
    return _session_locks([(_PENDING_PUSH_LOCK_ID, 0.0)])


logger = logbook(__name__)

# The provider the canonical services push tenants, users and assignments to.
# Fixed for the process; main.lifespan refuses a value not in SYNC_ADAPTERS and
# opens what the chosen adapter needs (app.state.keycloak_admin for keycloak,
# app.state.zitadel_admin for zitadel).
SYNC_PROVIDER = knob("SYNC_PROVIDER", "descope").strip().lower()


def _descope_access_keys(request: Request, _session: AsyncSession | None) -> MachineCredentials:
    """Descope's access keys, for a Descope principal only: they hand Descope the tenant id (conventions §18)."""
    require_descope_principal(request)
    return DescopeAccessKeys(get_descope_management(request))


def _keycloak_machine_credentials(request: Request, session: AsyncSession | None) -> MachineCredentials:
    """Canonical machine credentials, each a Keycloak client the sync adapter keeps.

    Their registration tokens are sealed under ``KEYCLOAK_REGISTRATION_TOKEN_KEYS``
    (#698); without it this raises ``RegistrationTokenKeysNotConfigured`` (startup
    already refuses to run without it).
    """
    if session is None:
        raise RuntimeError("machine credentials of a canonical provider need DATABASE_URL")
    return CanonicalMachineCredentials(
        provider="keycloak",
        repository=MachineCredentialRepository(session, cipher=registration_token_cipher()),
        push=build_keycloak_sync_adapter(request.app.state.keycloak_admin, session),
    )


@dataclass(frozen=True)
class SyncAdapterRegistration:
    """A provider's sync adapter: its class, which declares its capabilities and its default
    relationship store, how it is built, and which principals are the provider's own
    (their subject and tenant are its ids).

    ``machine_credentials`` builds the provider's ``MachineCredentials`` when its
    adapter declares ``Capability.MACHINE_CREDENTIALS``, and is ``None`` otherwise
    (``test_capabilities.py`` holds the two together).
    """

    adapter: type[IdentityProviderAdapter]
    build: Callable[[State, AsyncSession], IdentityProviderAdapter]
    own_principal: Callable[[Request], bool]
    machine_credentials: Callable[[Request, AsyncSession | None], MachineCredentials] | None = None


def _recorded_as(name: str) -> Callable[[Request], bool]:
    """Whether a middleware attributed the request's claims to the provider named ``name``."""

    def recorded(request: Request) -> bool:
        provider = getattr(request.state, "provider", None)
        return isinstance(provider, ProviderTokenConfig) and provider.name == name

    return recorded


# One entry per provider: its adapter's class, how the adapter is built from the
# application state and the request's database session, and its own principals.
SYNC_ADAPTERS: dict[str, SyncAdapterRegistration] = {
    "descope": SyncAdapterRegistration(
        DescopeSyncAdapter,
        lambda state, _session: DescopeSyncAdapter(client=state.descope_client),
        is_descope_principal,
        machine_credentials=_descope_access_keys,
    ),
    "keycloak": SyncAdapterRegistration(
        KeycloakSyncAdapter,
        lambda state, session: build_keycloak_sync_adapter(state.keycloak_admin, session),
        _recorded_as(KEYCLOAK_PROVIDER_NAME),
        machine_credentials=_keycloak_machine_credentials,
    ),
    "zitadel": SyncAdapterRegistration(
        ZitadelSyncAdapter,
        lambda state, session: build_zitadel_sync_adapter(state.zitadel_admin, session),
        _recorded_as(ZITADEL_PROVIDER_NAME),
    ),
}


# One entry per key of SYNC_ADAPTERS: the name of the provider whose principals the
# canonical services push writes for, as ``require_sync_provider_principal`` names it
# in its refusal. Who those principals are is ``is_sync_provider_principal``.
SYNC_PROVIDER_NAMES: dict[str, str] = {
    "descope": "Descope",
    "keycloak": KEYCLOAK_PROVIDER_NAME,
    "zitadel": ZITADEL_PROVIDER_NAME,
}


# Reconciliation runs one pass per configured provider: the provider the canonical
# model is pushed to (SYNC_PROVIDER), when it has a reader. A provider with none
# (Ory, Zitadel) is refused.
RECONCILIATION_HAS_NO_READER = "Reconciliation has no reader for SYNC_PROVIDER; it reads descope and keycloak"


def get_sync_adapter(request: Request, session: AsyncSession) -> IdentityProviderAdapter:
    """The sync adapter of the configured provider (``SYNC_PROVIDER``)."""
    return SYNC_ADAPTERS[SYNC_PROVIDER].build(request.app.state, session)


def sync_capabilities() -> frozenset[Capability]:
    """The optional capabilities the configured provider's sync adapter declares."""
    return SYNC_ADAPTERS[SYNC_PROVIDER].adapter.capabilities


# The relationship store a deployment selects (RelationshipStoreKind). Unset, the
# sync provider's registration chooses. Fixed for the process; main.lifespan refuses
# a value that names no store.
RELATIONSHIP_STORE = knob("RELATIONSHIP_STORE", "").strip().lower()


@dataclass(frozen=True)
class RelationshipStoreRegistration:
    """A relationship store: whom it serves, how it is built, and the user directory of its subjects.

    ``serves`` is whether the request's principal has the ids the store keys on:
    Descope's store keys on Descope tenant and user ids, so it serves Descope
    principals; OpenFGA keys on canonical ids, so it serves the principals
    authorized from the canonical model (conventions §18).
    """

    serves: Callable[[Request], bool]
    build: Callable[[State], RelationshipStore]
    users: Callable[[State, AsyncSession], UserDirectory]
    # Whether a relation may name a subject the store's directory does not find in
    # the caller's tenant. Descope's raw admin surface keys on whatever it is given;
    # OpenFGA's subjects are canonical users only, so anything else is refused.
    open_subjects: bool
    # Opens what the store needs for the life of the process, at startup, and
    # returns what closes it. ``None``: the store needs nothing of its own.
    start: Callable[[State], Awaitable[Callable[[], Awaitable[None]]]] | None = None


async def _start_openfga(state: State) -> Callable[[], Awaitable[None]]:
    """Connect OpenFGA (``connect_openfga``): a missing setting, an unreachable server or a
    model it does not hold stops startup (``RelationshipStoreNotAvailable``)."""
    http = outbound_client(timeout=OPENFGA_TIMEOUT_SECONDS)
    try:
        state.openfga = await connect_openfga(http)
    except Exception:
        await http.aclose()
        raise
    logger.info(
        "OpenFGA connected: store %s, authorization model %s",
        state.openfga.settings.store_id,
        state.openfga.settings.authorization_model_id,
    )

    async def stop() -> None:
        state.openfga = None
        await http.aclose()

    return stop


# One entry per store.
RELATIONSHIP_STORES: dict[RelationshipStoreKind, RelationshipStoreRegistration] = {
    RelationshipStoreKind.DESCOPE: RelationshipStoreRegistration(
        serves=is_descope_principal,
        build=lambda state: DescopeRelationshipStore(state.descope_client),
        users=lambda state, _session: DescopeUserDirectory(state.descope_client),
        open_subjects=True,
    ),
    RelationshipStoreKind.OPENFGA: RelationshipStoreRegistration(
        serves=is_canonical_principal,
        build=lambda state: OpenFgaRelationshipStore(state.openfga),
        users=lambda _state, session: CanonicalUserDirectory(
            UserRepository(session), UserTenantRoleRepository(session)
        ),
        open_subjects=False,
        start=_start_openfga,
    ),
}


def is_sync_provider_principal(request: Request) -> bool:
    """Whether the caller is a principal of the configured provider: its ``sub`` and tenant are that provider's."""
    return SYNC_ADAPTERS[SYNC_PROVIDER].own_principal(request)


def relationship_store_kind() -> RelationshipStoreKind:
    """The kind of the deployment's relationship store: ``RELATIONSHIP_STORE``, else the sync provider's.

    Raises ``RelationshipStoreNotAvailable`` for a ``RELATIONSHIP_STORE`` that names no store.
    """
    if not RELATIONSHIP_STORE:
        return SYNC_ADAPTERS[SYNC_PROVIDER].adapter.relationship_store
    try:
        kind = RelationshipStoreKind(RELATIONSHIP_STORE)
    except ValueError as exc:
        raise RelationshipStoreNotAvailable(RelationshipStoreFault.UNKNOWN, RELATIONSHIP_STORE) from exc
    return kind


async def start_relationship_store(state: State, kind: RelationshipStoreKind) -> Callable[[], Awaitable[None]] | None:
    """Start the deployment's store (its registration's ``start``) for ``main.lifespan``; what stops it, or ``None``."""
    registration = RELATIONSHIP_STORES.get(kind)
    if registration is None or registration.start is None:
        return None
    return await registration.start(state)


def relationship_store_registration() -> RelationshipStoreRegistration:
    """The deployment's store's registration; ``CapabilityMissing`` when it has none."""
    registration = RELATIONSHIP_STORES.get(relationship_store_kind())
    if registration is None:
        raise IdentityErrorRaised(CapabilityMissing(message="This deployment has no relationship store"))
    return registration


def get_relationship_store(request: Request) -> RelationshipStore:
    """The deployment's relationship store. The FGA and document routes reach relations only through it.

    Their ``require_relationship_store`` gate refuses a deployment with none before this runs.
    """
    return relationship_store_registration().build(request.app.state)


def get_user_directory(request: Request, session: AsyncSession = Depends(get_async_session)) -> UserDirectory:
    """The directory of the users the deployment's relationship store names (``app.services.relationship_users``)."""
    return relationship_store_registration().users(request.app.state, session)


async def get_machine_credential_service(
    request: Request,
    session: AsyncSession | None = Depends(get_rbac_session),
) -> MachineCredentialService:
    """The configured provider's machine credentials (``/api/keys``).

    The route's capability gate runs first, so a provider without the capability
    never gets here; one that does with no builder is a configuration defect.
    The session is ``get_rbac_session``'s, ``None`` without a database: Descope's
    access keys need none, as before.
    """
    build = SYNC_ADAPTERS[SYNC_PROVIDER].machine_credentials
    if build is None:
        raise RuntimeError(f"SYNC_PROVIDER={SYNC_PROVIDER} declares no machine credentials")
    return MachineCredentialService(build(request, session))


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


async def get_member_directory(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> MemberDirectory:
    """The tenant members the ``/api/members`` routes read and change, by ``SYNC_PROVIDER``.

    Descope (the default): the Descope management adapter, which keeps members in
    Descope under the Descope tenant and user ids its principals carry (503 until
    the client is built). Every other provider: ``MemberService`` over the
    canonical users and memberships, pushed through the provider's sync adapter.
    """
    if SYNC_PROVIDER == "descope":
        return require_descope_management(request)
    return MemberService(
        users=await get_user_service(request, session),
        roles=await get_role_service(request, session),
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
        run_repository=ReconciliationRunRepository(session),
        # The marks mean something only where a pass consumes them.
        pending_push_repository=PendingPushRepository(session) if SYNC_PROVIDER in RECONCILIATION_PASSES else None,
    )


async def get_reconciliation_service(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> ReconciliationService:
    """Build a ReconciliationService with the pass of every configured provider.

    AC-3.2.1: Wiring: AsyncSession -> all identity repositories + the provider's reader
               -> ReconciliationService(session, acquire_lock, descope_client | passes, repositories...)
    Advisory lock callable injected here to keep SA imports out of the domain service.
    The configured provider is ``SYNC_PROVIDER``: Descope's pass reads the Descope
    Management API, Keycloak's the realm (``app.state.keycloak_admin``). Refused
    (409) for a provider with no reader.
    """
    if SYNC_PROVIDER != "descope" and SYNC_PROVIDER not in RECONCILIATION_PASSES:
        raise IdentityErrorRaised(Conflict(message=RECONCILIATION_HAS_NO_READER))

    async def _acquire_lock() -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _RECONCILIATION_LOCK_ID},
        )

    run_repository = ReconciliationRunRepository(session)
    if SYNC_PROVIDER == "descope":
        return ReconciliationService(
            session=session,
            acquire_lock=_acquire_lock,
            descope_client=request.app.state.descope_client,
            user_repository=UserRepository(session),
            role_repository=RoleRepository(session),
            permission_repository=PermissionRepository(session),
            tenant_repository=TenantRepository(session),
            idp_link_repository=IdPLinkRepository(session),
            provider_repository=ProviderRepository(session),
            publisher=request.app.state.cache_publisher,
            run_repository=run_repository,
        )
    return ReconciliationService(
        session=session,
        acquire_lock=_acquire_lock,
        passes=[RECONCILIATION_PASSES[SYNC_PROVIDER](request.app.state, session, _acquire_lock)],
        publisher=request.app.state.cache_publisher,
        run_repository=run_repository,
    )


# One entry per provider, besides Descope, whose state reconciliation reads: how its
# pass is built from the application state, the request's session and the
# reconciliation lock. Descope's pass is built by ReconciliationService from the
# Descope client. A Zitadel or Ory reader is one more entry.
RECONCILIATION_PASSES: dict[str, Callable[[State, AsyncSession, Callable], ProviderReconciliation]] = {
    "keycloak": lambda state, session, lock: build_keycloak_reconciliation(
        state.keycloak_admin, session, lock, hold_reconciliation_pass
    ),
}


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
