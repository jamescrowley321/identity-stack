"""Wiring for ZitadelSyncAdapter: its settings, its Zitadel client and its canonical links.

Used when ``SYNC_PROVIDER`` is ``zitadel`` (``app.dependencies.identity``). The settings
are read through ``app.knobs`` once, at startup, and a configuration that is not complete
stops startup (``ZitadelSyncNotConfigured``), naming the variable and never its value.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.knobs import knob
from app.models.identity.user import IdPLink
from app.outbound import outbound_client
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.tenant import TenantRepository
from app.services.adapters.zitadel import PROVIDER_NAME, ApiOpener, ZitadelSyncAdapter
from app.services.adapters.zitadel_api import ZitadelAdminApi

ISSUER_VARIABLE = "ZITADEL_ISSUER_URL"
PROJECT_VARIABLE = "ZITADEL_AUDIENCE"
ADMIN_CREDENTIAL_VARIABLE = "ZITADEL_ADMIN_TOKEN"
ADMIN_TIMEOUT_VARIABLE = "ZITADEL_ADMIN_TIMEOUT_SECONDS"


class ZitadelSyncNotConfigured(RuntimeError):
    """``SYNC_PROVIDER`` is ``zitadel`` but a setting the adapter needs is missing or invalid."""


@dataclass(frozen=True)
class ZitadelAdminSettings:
    """Where the adapter calls Zitadel, as whom, for which project, and how long it waits."""

    base_url: str
    project_id: str
    token: str = field(repr=False)
    timeout_seconds: float


def zitadel_admin_settings() -> ZitadelAdminSettings:
    """Read the adapter's settings, or raise ``ZitadelSyncNotConfigured`` naming what is missing.

    The instance is the issuer (``ZITADEL_ISSUER_URL``), the project is the audience
    (``ZITADEL_AUDIENCE``), and the credential is a personal access token of a machine
    user with the instance role ``IAM_ORG_MANAGER`` (``ZITADEL_ADMIN_TOKEN``).
    """
    values = {
        ISSUER_VARIABLE: knob("ZITADEL_ISSUER_URL") or "",
        PROJECT_VARIABLE: knob("ZITADEL_AUDIENCE") or "",
        ADMIN_CREDENTIAL_VARIABLE: knob("ZITADEL_ADMIN_TOKEN") or "",
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ZitadelSyncNotConfigured(f"SYNC_PROVIDER is zitadel but {', '.join(missing)} is not set")
    raw_timeout = knob("ZITADEL_ADMIN_TIMEOUT_SECONDS", "10")
    try:
        timeout = float(raw_timeout)
    except ValueError:
        timeout = math.nan
    if not (math.isfinite(timeout) and timeout > 0):
        raise ZitadelSyncNotConfigured(f"{ADMIN_TIMEOUT_VARIABLE} must be a positive number of seconds")
    return ZitadelAdminSettings(
        base_url=values[ISSUER_VARIABLE].rstrip("/"),
        project_id=values[PROJECT_VARIABLE],
        token=values[ADMIN_CREDENTIAL_VARIABLE],
        timeout_seconds=timeout,
    )


def admin_api_opener(settings: ZitadelAdminSettings) -> ApiOpener:
    """Opens a ZitadelAdminApi on a client of its own, closed when the sync operation ends."""

    @asynccontextmanager
    async def open_api() -> AsyncIterator[ZitadelAdminApi]:
        async with outbound_client(timeout=settings.timeout_seconds) as http:
            yield ZitadelAdminApi(http, base_url=settings.base_url, token=settings.token)

    return open_api


class TenantLinkedElsewhere(Exception):
    """The tenant is already linked to an organization of another provider, or to another organization."""


class RepositoryZitadelLinks:
    """The adapter's canonical side, in the request's session. Each write commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._providers = ProviderRepository(session)
        self._tenants = TenantRepository(session)
        self._idp_links = IdPLinkRepository(session)

    async def provider_id(self) -> uuid.UUID | None:
        provider = await self._providers.get_by_name(PROVIDER_NAME)
        return None if provider is None else provider.id

    async def organization_of(self, tenant_id: uuid.UUID, provider_id: uuid.UUID) -> str | None:
        tenant = await self._tenants.get(tenant_id)
        if tenant is None or tenant.external_org_provider_id != provider_id:
            return None
        return tenant.external_org_id

    async def link_organization(self, tenant_id: uuid.UUID, provider_id: uuid.UUID, org_id: str) -> None:
        """Record ``org_id`` as the tenant's organization. A tenant linked to anything else is left as it is."""
        tenant = await self._tenants.get(tenant_id)
        if tenant is None:
            raise LookupError(f"Tenant {tenant_id} not found")
        if tenant.external_org_id is not None and (tenant.external_org_id, tenant.external_org_provider_id) != (
            org_id,
            provider_id,
        ):
            raise TenantLinkedElsewhere(f"Tenant {tenant_id} is already linked to another organization")
        tenant.external_org_id = org_id
        tenant.external_org_provider_id = provider_id
        await self._tenants.update(tenant)
        await self._tenants.commit()

    async def subject_of(self, user_id: uuid.UUID, provider_id: uuid.UUID) -> str | None:
        for link in await self._idp_links.get_by_user(user_id):
            if link.provider_id == provider_id:
                return link.external_sub
        return None

    async def link_subject(self, user_id: uuid.UUID, provider_id: uuid.UUID, sub: str, email: str) -> None:
        await self._idp_links.create(
            IdPLink(user_id=user_id, provider_id=provider_id, external_sub=sub, external_email=email)
        )
        await self._idp_links.commit()


def build_zitadel_sync_adapter(settings: ZitadelAdminSettings, session: AsyncSession) -> ZitadelSyncAdapter:
    """The adapter for one request: Zitadel through ``settings``, the canonical model through ``session``."""
    return ZitadelSyncAdapter(
        admin_api_opener(settings), RepositoryZitadelLinks(session), project_id=settings.project_id
    )
