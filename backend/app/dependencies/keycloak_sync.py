"""Wiring for KeycloakSyncAdapter: its canonical links, in the request's session.

Used when ``SYNC_PROVIDER`` is ``keycloak`` (``app.dependencies.identity``). The
admin client itself is opened once at startup (``main.lifespan``,
``app.state.keycloak_admin``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from app.logbook import logbook
from app.models.identity.user import LINK_ORIGIN, LINKED_AT_SIGN_IN, LINKED_BY_BACKEND, IdPLink
from app.repositories.base import RepositoryConflictError
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.services.adapters.keycloak import (
    ORGANIZATION_HOLD_SECONDS,
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

# Namespaces the advisory-lock key of a Keycloak organization (hashed with its alias).
ORGANIZATION_LOCK_NAMESPACE = "keycloak-organization:"
# How long a push waits for an organization's lock (``lock_timeout``, whole milliseconds):
# longer than one holder may keep it (``ORGANIZATION_HOLD_SECONDS``, the deadline of its
# Keycloak calls) plus a margin for its database work, so a push queued behind one holder
# gets the lock. Bounded all the same: each waiter holds its session's connection. A push
# queued behind several holders can still give up, a failed push.
ORGANIZATION_LOCK_MARGIN_SECONDS = 5
ORGANIZATION_LOCK_TIMEOUT_MS = round((ORGANIZATION_HOLD_SECONDS + ORGANIZATION_LOCK_MARGIN_SECONDS) * 1000)

# The link origins the adapter follows to a Keycloak account, as the Zitadel
# adapter's: a sign-in's, and the backend's own (an account the adapter created,
# or one an operator's seed names). An admin-made link is not followed: the admin
# client may change any account of the realm, and that link names whatever
# account the admin typed in.
FOLLOWED_LINK_ORIGINS = frozenset({LINKED_AT_SIGN_IN, LINKED_BY_BACKEND})


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
        self._roles = RoleRepository(session)

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
        return await self._read(
            "the IdP link read", lambda: self._idp_links.sub_made_by(user_id, PROVIDER_NAME, FOLLOWED_LINK_ORIGINS)
        )

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

    async def role(self, role_id: uuid.UUID) -> tuple[str, uuid.UUID | None] | None:
        async def read() -> tuple[str, uuid.UUID | None] | None:
            row = await self._roles.get(role_id)
            return None if row is None else (row.name, row.tenant_id)

        return await self._read("the role read", read)

    @asynccontextmanager
    async def organization_lock(self, alias: str) -> AsyncIterator[None]:
        """A Postgres advisory lock per organization alias, on the request's session.

        A transaction lock taken in a savepoint and released by rolling the savepoint
        back, whether the body succeeded or raised: rolling back to a savepoint
        releases the locks taken after it and expires no row the body did not change.
        The wait is bounded by ``ORGANIZATION_LOCK_TIMEOUT_MS`` (``lock_timeout``, local
        to the savepoint). A wait that runs out, or any database failure taking the
        lock, is ``LocalDatabaseError``. A failure releasing it is logged: the lock ends
        with the request's transaction at the latest.
        """
        try:
            savepoint = await self._session.begin_nested()
        except DATABASE_FAILURES as exc:
            raise LocalDatabaseError(failed("the organization lock", exc)) from exc
        try:
            await self._session.execute(
                sa.select(sa.func.set_config("lock_timeout", f"{ORGANIZATION_LOCK_TIMEOUT_MS}ms", True))
            )
            await self._session.execute(
                sa.select(
                    sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(ORGANIZATION_LOCK_NAMESPACE + alias, 0))
                )
            )
        except DATABASE_FAILURES as exc:
            await _release(savepoint, alias)
            raise LocalDatabaseError(failed("the organization lock", exc)) from exc
        except BaseException:
            # A cancellation mid-wait: leave no savepoint holding the lock or lock_timeout.
            await _release(savepoint, alias)
            raise
        try:
            yield
        finally:
            await _release(savepoint, alias)

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
        except RepositoryConflictError as exc:
            await self._rollback()
            raise LinkRefused(f"tenant {tenant_id} could not be linked: another write conflicts") from exc
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
        except RepositoryConflictError as exc:
            # A sign-in or a concurrent push wrote a link first: the same one counts as written.
            await self._rollback()
            existing = await self._read(
                "the IdP link read", lambda: self._idp_links.get_by_provider_and_sub(provider_id, sub)
            )
            if existing is not None and existing.user_id == user_id:
                return
            raise LinkRefused("the user or the Keycloak account already has another link") from exc
        except DATABASE_FAILURES as exc:
            await self._rollback()
            raise LocalDatabaseError(failed("the IdP link write", exc)) from exc

    async def _rollback(self) -> None:
        try:
            await self._session.rollback()
        except DATABASE_FAILURES as exc:
            # The write's own failure is what the push reports.
            logger.warning("Keycloak link write: the rollback failed too: %s", type(exc).__name__)


async def _release(savepoint: AsyncSessionTransaction, alias: str) -> None:
    """Roll the organization lock's savepoint back, which releases the lock; a failure is logged, never raised."""
    try:
        await savepoint.rollback()
    except DATABASE_FAILURES as exc:
        logger.warning(
            "Keycloak organization lock for %s not released before the request's transaction ends: %s",
            alias,
            type(exc).__name__,
        )


def build_keycloak_sync_adapter(api: KeycloakAdminApi, session: AsyncSession) -> KeycloakSyncAdapter:
    """The adapter for one request: Keycloak through ``api``, the canonical model through ``session``."""
    return KeycloakSyncAdapter(api, RepositoryKeycloakLinks(session))
