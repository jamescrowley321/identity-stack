"""Wiring for Keycloak's reconciliation pass, in the request's session.

Used when ``SYNC_PROVIDER`` is ``keycloak`` (``app.dependencies.identity``), on the
admin client ``main.lifespan`` opened (``app.state.keycloak_admin``).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.keycloak_sync import build_keycloak_sync_adapter
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.keycloak_pull import KeycloakPullStateRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.sync_event import SyncEventRepository
from app.repositories.sync_ledger import CanonicalStateRepository, PendingPushRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.inbound_sync import InboundSyncService, KeycloakPullRepositories
from app.services.keycloak_admin import KeycloakAdminApi
from app.services.keycloak_reconciliation import KeycloakReconciliation


def build_keycloak_reconciliation(
    api: KeycloakAdminApi,
    session: AsyncSession,
    acquire_lock: Callable[[], Coroutine[Any, Any, None]],
) -> KeycloakReconciliation:
    """The Keycloak pass: Keycloak through ``api``, the canonical model through ``session``."""
    inbound = InboundSyncService(
        user_repository=UserRepository(session),
        idp_link_repository=IdPLinkRepository(session),
        provider_repository=ProviderRepository(session),
        sync_event_repository=SyncEventRepository(session),
        keycloak_repositories=KeycloakPullRepositories(
            tenant=TenantRepository(session),
            role=RoleRepository(session),
            permission=PermissionRepository(session),
            assignment=UserTenantRoleRepository(session),
        ),
    )
    return KeycloakReconciliation(
        session=session,
        acquire_lock=acquire_lock,
        api=api,
        adapter=build_keycloak_sync_adapter(api, session),
        inbound=inbound,
        state=CanonicalStateRepository(session),
        ledger=PendingPushRepository(session),
        pull_state=KeycloakPullStateRepository(session),
        providers=ProviderRepository(session),
        events=SyncEventRepository(session),
    )
