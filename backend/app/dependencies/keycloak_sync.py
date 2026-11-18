"""Wiring for KeycloakSyncAdapter: its canonical links, in the request's session.

Used when ``SYNC_PROVIDER`` is ``keycloak`` (``app.dependencies.identity``). The
admin client itself is opened once at startup (``main.lifespan``,
``app.state.keycloak_admin``).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.logbook import logbook
from app.models.identity.user import LINK_ORIGIN, LINKED_BY_BACKEND, IdPLink
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.tenant import TenantRepository
from app.services.adapters.keycloak import (
    PROVIDER_NAME,
    KeycloakLinks,
    KeycloakSyncAdapter,
    LinkRefused,
    LocalDatabaseError,
)
from app.services.keycloak_admin import KeycloakAdminApi

logger = logbook(__name__)

# What a database call raises when the database or the pool fails: SQLAlchemy's
# errors (the driver's wrapped, a pool checkout timeout) and a refused connection.
DATABASE_FAILURES = (SQLAlchemyError, OSError)

T = TypeVar("T")


def failed(what: str, exc: BaseException) -> str:
    """A LocalDatabaseError's message: the exception's type only, never the statement or its parameters."""
    return f"{what} failed in the service's database: {type(exc).__name__}"


class RepositoryKeycloakLinks(KeycloakLinks):
    """The adapter's canonical side, in the request's session.

    Reads run in a savepoint of the request's transaction: a failure rolls back
    the savepoint only, so the rows the request loaded stay loaded. Each write
    commits, and one that fails is rolled back: the service has already committed
    its own change. A database failure is ``LocalDatabaseError``; a write the
    canonical side refuses is ``LinkRefused``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._providers = ProviderRepository(session)
        self._tenants = TenantRepository(session)
        self._idp_links = IdPLinkRepository(session)

    async def _read(self, what: str, read: Callable[[], Awaitable[T]]) -> T:
        try:
            async with self._session.begin_nested():
                return await read()
        except DATABASE_FAILURES as exc:
            raise LocalDatabaseError(failed(what, exc)) from exc

    async def _provider_id(self) -> uuid.UUID | None:
        provider = await self._providers.get_by_name(PROVIDER_NAME)
        return None if provider is None else provider.id

    async def linked_subject(self, user_id: uuid.UUID) -> str | None:
        return await self._read("the IdP link read", lambda: self._idp_links.signed_in_sub_of(user_id, PROVIDER_NAME))

    async def has_link(self, user_id: uuid.UUID) -> bool:
        async def read() -> bool:
            provider_id = await self._provider_id()
            links = await self._idp_links.get_by_user(user_id)
            return provider_id is not None and any(link.provider_id == provider_id for link in links)

        return await self._read("the IdP link read", read)

    async def organization_alias(self, tenant_id: uuid.UUID) -> str | None:
        async def read() -> str | None:
            provider_id = await self._provider_id()
            tenant = await self._tenants.get(tenant_id)
            if provider_id is None or tenant is None or tenant.external_org_provider_id != provider_id:
                return None
            return tenant.external_org_id

        return await self._read("the tenant link read", read)

    async def linked_elsewhere(self, tenant_id: uuid.UUID) -> bool:
        async def read() -> bool:
            provider_id = await self._provider_id()
            tenant = await self._tenants.get(tenant_id)
            return (
                tenant is not None
                and tenant.external_org_id is not None
                and (provider_id is None or tenant.external_org_provider_id != provider_id)
            )

        return await self._read("the tenant link read", read)

    async def link_organization(self, tenant_id: uuid.UUID, alias: str) -> None:
        """Record ``alias`` as the tenant's organization. A tenant linked to anything else is refused."""
        try:
            provider_id = await self._provider_id()
            if provider_id is None:
                raise LinkRefused(f"no {PROVIDER_NAME!r} provider row: run the Keycloak seed first")
            tenant = await self._tenants.get(tenant_id)
            if tenant is None:
                raise LinkRefused(f"tenant {tenant_id} not found")
            if (tenant.external_org_id, tenant.external_org_provider_id) == (alias, provider_id):
                return
            if tenant.external_org_id is not None:
                raise LinkRefused(f"tenant {tenant_id} is already linked to another organization")
            tenant.external_org_id = alias
            tenant.external_org_provider_id = provider_id
            await self._tenants.update(tenant)
            await self._tenants.commit()
        except DATABASE_FAILURES as exc:
            await self._rollback()
            raise LocalDatabaseError(failed("the tenant link write", exc)) from exc

    async def link_user(self, user_id: uuid.UUID, sub: str, email: str) -> None:
        """Link the user to the account the adapter created; the same link already there counts as written."""
        try:
            provider_id = await self._provider_id()
            if provider_id is None:
                raise LinkRefused(f"no {PROVIDER_NAME!r} provider row: run the Keycloak seed first")
            existing = await self._idp_links.get_by_provider_and_sub(provider_id, sub)
            if existing is not None:
                if existing.user_id != user_id:
                    raise LinkRefused("the Keycloak account is linked to another canonical user")
                return
            await self._idp_links.create(
                IdPLink(
                    user_id=user_id,
                    provider_id=provider_id,
                    external_sub=sub,
                    external_email=email,
                    metadata_={LINK_ORIGIN: LINKED_BY_BACKEND},
                )
            )
            await self._idp_links.commit()
        except DATABASE_FAILURES as exc:
            await self._rollback()
            raise LocalDatabaseError(failed("the IdP link write", exc)) from exc

    async def _rollback(self) -> None:
        try:
            await self._session.rollback()
        except DATABASE_FAILURES as exc:
            # The write's own failure is what the push reports.
            logger.warning("Keycloak link write: the rollback failed too: %s", type(exc).__name__)


def build_keycloak_sync_adapter(api: KeycloakAdminApi, session: AsyncSession) -> KeycloakSyncAdapter:
    """The adapter for one request: Keycloak through ``api``, the canonical model through ``session``."""
    return KeycloakSyncAdapter(api, RepositoryKeycloakLinks(session))
