"""Wiring for Keycloak's reconciliation pass, in the request's session, and the loop that retries pushes.

Used when ``SYNC_PROVIDER`` is ``keycloak`` (``app.dependencies.identity``), on the
admin client ``main.lifespan`` opened (``app.state.keycloak_admin``). ``main.lifespan``
also starts ``run_keycloak_push_retry`` as a background task: every
``KEYCLOAK_PUSH_RETRY_INTERVAL_SECONDS`` it pushes the pending pushes again, so a
push that failed (a membership removal, a deactivation) reaches Keycloak without
waiting for a reconciliation run (#612). Every worker runs the loop; the
pending-push lock (``hold_push_retry``) lets one push at a time, and a pass waits for it.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.keycloak_sync import RepositoryKeycloakLinks
from app.errors.identity import Conflict
from app.knobs import knob
from app.logbook import logbook
from app.models.database import get_session_factory
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
from app.services.adapters.keycloak import KeycloakSyncAdapter
from app.services.inbound_sync import InboundSyncService, KeycloakPullRepositories
from app.services.keycloak_admin import KeycloakAdminApi
from app.services.keycloak_reconciliation import KeycloakReconciliation

logger = logbook(__name__)

RETRY_INTERVAL_VARIABLE = "KEYCLOAK_PUSH_RETRY_INTERVAL_SECONDS"


class KeycloakPushRetryNotConfigured(ValueError):
    """``KEYCLOAK_PUSH_RETRY_INTERVAL_SECONDS`` is not a positive number of seconds."""


def keycloak_push_retry_interval() -> float:
    """Seconds between two retries of the pending pushes. Raises ``KeycloakPushRetryNotConfigured``."""
    raw = knob("KEYCLOAK_PUSH_RETRY_INTERVAL_SECONDS", "60")
    try:
        interval = float(raw)
    except ValueError:
        interval = math.nan
    if not math.isfinite(interval) or interval <= 0:
        raise KeycloakPushRetryNotConfigured(f"{RETRY_INTERVAL_VARIABLE} must be a positive number of seconds")
    return interval


def build_keycloak_reconciliation(
    api: KeycloakAdminApi,
    session: AsyncSession,
    acquire_lock: Callable[[], Coroutine[Any, Any, None]],
    hold_pass: Callable[[], AbstractAsyncContextManager[bool]],
) -> KeycloakReconciliation:
    """The Keycloak pass: Keycloak through ``api``, the canonical model through ``session``."""
    links = RepositoryKeycloakLinks(session)
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
        hold_pass=hold_pass,
        api=api,
        adapter=KeycloakSyncAdapter(api, links),
        links=links,
        inbound=inbound,
        state=CanonicalStateRepository(session),
        ledger=PendingPushRepository(session),
        pull_state=KeycloakPullStateRepository(session),
        providers=ProviderRepository(session),
        events=SyncEventRepository(session),
    )


async def _no_reconciliation_lock() -> None:
    """``push_pending`` reads nothing of the realm: it takes the pass lock alone."""


async def retry_keycloak_pushes(
    api: KeycloakAdminApi, session: AsyncSession, hold_pass: Callable[[], AbstractAsyncContextManager[bool]]
) -> None:
    """Push the pending pushes once; a pass already running pushes them itself."""
    reconciliation = build_keycloak_reconciliation(api, session, _no_reconciliation_lock, hold_pass)
    result = await reconciliation.push_pending()
    if result.is_error():
        if not isinstance(result.error, Conflict):
            logger.warning("Keycloak push retry: %s", result.error.message)
        return
    if result.ok["push_failed"]:
        logger.warning("Keycloak push retry: %s", result.ok)


async def run_keycloak_push_retry(
    api: KeycloakAdminApi, interval_seconds: float, hold_pass: Callable[[], AbstractAsyncContextManager[bool]]
) -> None:
    """Retry the pending pushes every ``interval_seconds`` until cancelled; a failed retry is logged."""
    factory = get_session_factory()
    logger.info("Keycloak push retry: pending pushes pushed every %ss", interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with factory() as session:
                await retry_keycloak_pushes(api, session, hold_pass)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Keycloak push retry failed")
