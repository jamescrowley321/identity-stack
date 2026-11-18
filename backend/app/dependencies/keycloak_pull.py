"""Wiring for the Keycloak event pull: its settings, its service per poll, and the loop that polls.

Runs when ``SYNC_PROVIDER`` is ``keycloak`` and ``KEYCLOAK_PULL_EVENTS`` is on:
``main.lifespan`` starts ``run_keycloak_pull`` as a background task on the admin
client it opened, and cancels it at shutdown. Every worker runs the loop; the
pull's advisory lock lets one poll at a time (``KeycloakPullService``).
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.knobs import knob, knob_flag
from app.logbook import logbook
from app.models.database import get_session_factory
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.keycloak_pull import KeycloakPullStateRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.sync_event import SyncEventRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.cache_invalidation import CacheInvalidationPublisher
from app.services.inbound_sync import InboundSyncService, KeycloakPullRepositories
from app.services.keycloak_admin import KeycloakAdminApi
from app.services.keycloak_events import KeycloakEventReader
from app.services.keycloak_pull import KeycloakPullService

logger = logbook(__name__)

ENABLED_VARIABLE = "KEYCLOAK_PULL_EVENTS"
INTERVAL_VARIABLE = "KEYCLOAK_PULL_INTERVAL_SECONDS"
PAGE_SIZE_VARIABLE = "KEYCLOAK_PULL_PAGE_SIZE"
# Keycloak answers at most this many events a page whatever is asked.
MAX_PAGE_SIZE = 1000


class KeycloakPullNotConfigured(ValueError):
    """A pull setting has a value the pull cannot run with."""


@dataclass(frozen=True)
class KeycloakPullSettings:
    enabled: bool
    interval_seconds: float
    page_size: int


def keycloak_pull_settings() -> KeycloakPullSettings:
    """Read the pull's settings. Raises ``KeycloakPullNotConfigured`` for an interval or page size out of range."""
    raw_interval = knob("KEYCLOAK_PULL_INTERVAL_SECONDS", "10")
    raw_page_size = knob("KEYCLOAK_PULL_PAGE_SIZE", "100")
    try:
        interval = float(raw_interval)
    except ValueError:
        interval = math.nan
    if not math.isfinite(interval) or interval <= 0:
        raise KeycloakPullNotConfigured(f"{INTERVAL_VARIABLE} must be a positive number of seconds")
    try:
        page_size = int(raw_page_size)
    except ValueError:
        page_size = 0
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise KeycloakPullNotConfigured(f"{PAGE_SIZE_VARIABLE} must be a whole number from 1 to {MAX_PAGE_SIZE}")
    return KeycloakPullSettings(
        enabled=knob_flag("KEYCLOAK_PULL_EVENTS", "true"), interval_seconds=interval, page_size=page_size
    )


def build_keycloak_pull_service(
    api: KeycloakAdminApi,
    session: AsyncSession,
    *,
    page_size: int,
    publisher: CacheInvalidationPublisher | None = None,
) -> KeycloakPullService:
    """One poll's service: Keycloak through ``api``, the canonical model through ``session``."""
    inbound = InboundSyncService(
        user_repository=UserRepository(session),
        idp_link_repository=IdPLinkRepository(session),
        provider_repository=ProviderRepository(session),
        sync_event_repository=SyncEventRepository(session),
        publisher=publisher,
        keycloak_repositories=KeycloakPullRepositories(
            tenant=TenantRepository(session),
            role=RoleRepository(session),
            permission=PermissionRepository(session),
            assignment=UserTenantRoleRepository(session),
        ),
    )
    return KeycloakPullService(
        session=session,
        api=api,
        reader=KeycloakEventReader(api),
        inbound=inbound,
        state_repository=KeycloakPullStateRepository(session),
        provider_repository=ProviderRepository(session),
        page_size=page_size,
        publisher=publisher,
    )


async def run_keycloak_pull(
    api: KeycloakAdminApi,
    settings: KeycloakPullSettings,
    publisher: CacheInvalidationPublisher | None = None,
) -> None:
    """Poll every ``interval_seconds`` until cancelled; a failed poll is logged and the next one tries again."""
    factory = get_session_factory()
    logger.info("Keycloak pull: polling every %ss, %s events a page", settings.interval_seconds, settings.page_size)
    while True:
        try:
            async with factory() as session:
                service = build_keycloak_pull_service(api, session, page_size=settings.page_size, publisher=publisher)
                result = await service.poll()
            if result.is_error():
                logger.warning("Keycloak pull: the poll stopped: %s", result.error.message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Keycloak pull: the poll failed")
        await asyncio.sleep(settings.interval_seconds)
