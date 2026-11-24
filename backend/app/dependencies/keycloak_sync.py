"""Wiring for KeycloakSyncAdapter: its canonical links, in the request's session.

Used when ``SYNC_PROVIDER`` is ``keycloak`` (``app.dependencies.identity``). The
admin client itself is opened once at startup (``main.lifespan``,
``app.state.keycloak_admin``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import TypeVar

import anyio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction
from sqlalchemy.orm.attributes import set_committed_value

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
# How long releasing an organization's lock (a savepoint rollback, or the transaction's) may
# take: past it the session's connection is invalidated, which ends the transaction and the
# lock with it, so a hung connection cannot hold the lock or a cancelled push.
ORGANIZATION_LOCK_RELEASE_SECONDS = ORGANIZATION_HOLD_SECONDS + ORGANIZATION_LOCK_MARGIN_SECONDS

# Every row the session had loaded, with its loaded values: what a rollback expires.
Loaded = list[tuple[object, dict[str, object]]]

# The link origins the adapter follows to a Keycloak account, as the Zitadel
# adapter's: a sign-in's, and the backend's own (an account the adapter created,
# or one an operator's seed names). An admin-made link is not followed: the admin
# client may change any account of the realm, and that link names whatever
# account the admin typed in.
FOLLOWED_LINK_ORIGINS = frozenset({LINKED_AT_SIGN_IN, LINKED_BY_BACKEND})


class DatabaseFailure(StrEnum):
    """What a database failure under a push was, as its message names it."""

    CONNECTION_LOST = "the connection was lost"
    LOCK_TIMEOUT = "a lock was not taken within lock_timeout"
    QUERY_CANCELED = "the statement was canceled"


# SQLSTATEs (Postgres, appendix A) a failure is named by.
_FAILURE_BY_SQLSTATE = {
    "55P03": DatabaseFailure.LOCK_TIMEOUT,  # lock_not_available
    "57014": DatabaseFailure.QUERY_CANCELED,  # query_canceled: statement_timeout, a cancel request
}


def connection_lost(exc: BaseException) -> bool:
    """``exc`` dropped the session's connection: SQLAlchemy invalidated it, and the transaction with it."""
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


def database_failure(exc: BaseException) -> DatabaseFailure | None:
    """The kind of ``exc``, from the driver's SQLSTATE; ``None`` for any other failure."""
    if connection_lost(exc):
        return DatabaseFailure.CONNECTION_LOST
    if isinstance(exc, DBAPIError):
        return _FAILURE_BY_SQLSTATE.get(getattr(exc.orig, "sqlstate", None))
    return None


def failed(what: str, exc: BaseException) -> str:
    """A LocalDatabaseError's message: the failure's kind and type, never the statement or its parameters."""
    kind = database_failure(exc)
    detail = type(exc).__name__ if kind is None else f"{kind.value} ({type(exc).__name__})"
    return f"{what} failed in the service's database: {detail}"


class UncommittedWorkLost(Exception):
    """A push's database failure rolled back changes the request had not committed.

    Not a ``LocalDatabaseError``: the adapter does not turn it into a sync error,
    so the request fails rather than answer for changes that were not made.
    """


class RepositoryKeycloakLinks(KeycloakLinks):
    """The adapter's canonical side, in the request's session.

    Reads run in a savepoint of the request's transaction: a failure rolls back
    the savepoint only, so the rows the request loaded stay loaded. A read that
    loses the connection rolls back the transaction the loss ended, so the
    request's later statements run on a new connection. Each write commits, and
    one that fails is rolled back: the service has already committed its own
    change. After either rollback the rows the request loaded stay loaded; when
    the request had changes it had not committed, the rollback undid them and
    ``UncommittedWorkLost`` is raised. A database failure is ``LocalDatabaseError``; a write the
    canonical side refuses is ``LinkRefused``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._providers = ProviderRepository(session)
        self._tenants = TenantRepository(session)
        self._idp_links = IdPLinkRepository(session)
        self._roles = RoleRepository(session)

    async def _read(self, what: str, read: Callable[[], Awaitable[T]]) -> T:
        loaded: Loaded | None = None
        try:
            loaded = await self._loaded()
            async with self._session.begin_nested():
                return await read()
        except DATABASE_FAILURES as exc:
            if connection_lost(exc):
                await self._recover(loaded, what, exc)
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
        lock, is ``LocalDatabaseError``. A lost connection (a failure, or a cancellation
        mid-statement) rolls back the request's transaction instead, as a read's does, so
        the request's later statements run on a new connection. Releasing is bounded by
        ``ORGANIZATION_LOCK_RELEASE_SECONDS``; see ``_release``.
        """
        loaded: Loaded | None = None
        try:
            loaded = await self._loaded()
            savepoint = await self._session.begin_nested()
        except DATABASE_FAILURES as exc:
            if connection_lost(exc):
                await self._recover(loaded, "the organization lock", exc)
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
            if connection_lost(exc):
                await self._recover(loaded, "the organization lock", exc)
            else:
                await self._release(savepoint, alias, loaded, raise_lost=True)
            raise LocalDatabaseError(failed("the organization lock", exc)) from exc
        except BaseException:
            # A cancellation mid-wait: leave no savepoint holding the lock or lock_timeout.
            await self._release(savepoint, alias, loaded, raise_lost=False)
            raise
        # A cancellation unwinding through the body keeps propagating; any other outcome
        # gives way to UncommittedWorkLost when the release rolled back uncommitted work.
        raise_lost = True
        try:
            yield
        except BaseException as exc:
            raise_lost = isinstance(exc, Exception)
            raise
        finally:
            await self._release(savepoint, alias, loaded, raise_lost=raise_lost)

    async def link_organization(self, tenant_id: uuid.UUID, alias: str) -> None:
        """Record ``alias`` as the tenant's organization. A tenant linked to anything else is refused."""
        loaded: Loaded | None = None
        try:
            loaded = await self._loaded()
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
            await self._recover(loaded, "the tenant link write", exc)
            raise LinkRefused(f"tenant {tenant_id} could not be linked: another write conflicts") from exc
        except DATABASE_FAILURES as exc:
            await self._recover(loaded, "the tenant link write", exc)
            raise LocalDatabaseError(failed("the tenant link write", exc)) from exc

    async def link_user(self, user_id: uuid.UUID, sub: str, email: str) -> None:
        """Link the user to the account the adapter created; the same link already there counts as written."""
        loaded: Loaded | None = None
        try:
            loaded = await self._loaded()
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
            await self._recover(loaded, "the IdP link write", exc)
            existing = await self._read(
                "the IdP link read", lambda: self._idp_links.get_by_provider_and_sub(provider_id, sub)
            )
            if existing is not None and existing.user_id == user_id:
                return
            raise LinkRefused("the user or the Keycloak account already has another link") from exc
        except DATABASE_FAILURES as exc:
            await self._recover(loaded, "the IdP link write", exc)
            raise LocalDatabaseError(failed("the IdP link write", exc)) from exc

    async def _loaded(self) -> Loaded | None:
        """The rows the session has loaded, to put back after a rollback; ``None`` with work not committed.

        Work not committed is a change not flushed yet, or any write the open
        transaction made (Postgres assigns a transaction id at its first write,
        whether the ORM or a statement of the request's made it). With no
        transaction open, as after the service's commit, there is none to ask
        about: no statement is run, so a stale connection fails the push's own
        statement, not this one. When a transaction is open, the probe that asks
        whether it has written runs inside its own savepoint, so a failure rolls
        that savepoint back and leaves the outer transaction usable.
        """
        session = self._session.sync_session
        if session.new or session.deleted or any(session.is_modified(row) for row in session.dirty):
            return None
        if self._session.in_transaction():
            async with self._session.begin_nested():
                if await self._providers.transaction_has_written():
                    return None
        return [
            (row, {attr.key: attr.loaded_value for attr in state.attrs if attr.key not in state.unloaded})
            for state in session.identity_map.all_states()
            if (row := state.obj()) is not None
        ]

    async def _recover(self, loaded: Loaded | None, what: str, exc: BaseException) -> None:
        """Roll back the request's transaction and put back the rows ``loaded`` holds.

        A rollback expires every loaded row, and an expired row cannot load
        again outside an await. With no work of the request's uncommitted, a
        loaded row holds what the database holds. ``loaded`` is ``None`` when
        there was such work (or it could not be told): the rollback undid it,
        and ``UncommittedWorkLost`` says so.
        """
        await self._rollback()
        if loaded is None:
            raise UncommittedWorkLost(
                f"{failed(what, exc)}; the request's uncommitted changes were rolled back"
            ) from exc
        _put_back(loaded)

    async def _release(
        self, savepoint: AsyncSessionTransaction, alias: str, loaded: Loaded | None, *, raise_lost: bool
    ) -> None:
        """Roll the organization lock's savepoint back, which releases the lock.

        Shielded: it runs as a cancellation unwinds, too, so it is bounded by
        ``ORGANIZATION_LOCK_RELEASE_SECONDS``. When the savepoint cannot be rolled
        back (the connection lost, or a cancellation mid-statement left it
        unusable), the request's transaction is rolled back whole, which releases
        the lock as well, and the rows ``loaded`` holds are put back. When that
        rollback fails or runs out of time, the connection is invalidated, which
        ends the transaction and the lock. With work not committed (``loaded`` is
        ``None``) either undid it: ``UncommittedWorkLost`` is raised when
        ``raise_lost``, and logged otherwise (a cancellation is what propagates).
        """
        with anyio.CancelScope(shield=True):
            rolled_back = False
            try:
                with anyio.fail_after(ORGANIZATION_LOCK_RELEASE_SECONDS):
                    if await self._savepoint_rolled_back(savepoint, alias):
                        if loaded is not None:
                            _put_back(loaded)
                        return
                    await self._session.rollback()
                    rolled_back = True
            except DATABASE_FAILURES as exc:  # TimeoutError (fail_after) is an OSError
                logger.warning(
                    "Keycloak organization lock for %s: releasing the lock failed: %s", alias, type(exc).__name__
                )
                await self._discard_connection()
            if loaded is not None:
                if rolled_back:
                    _put_back(loaded)
                return
            message = f"Keycloak organization lock for {alias}: the request's uncommitted changes were rolled back"
            if raise_lost:
                raise UncommittedWorkLost(message)
            logger.warning(message)

    async def _savepoint_rolled_back(self, savepoint: AsyncSessionTransaction, alias: str) -> bool:
        """Roll ``savepoint`` back; ``False`` when its connection is lost or the rollback fails."""
        try:
            if (await self._session.connection()).invalidated:
                return False
            if savepoint.is_active:
                await savepoint.rollback()
        except DATABASE_FAILURES as exc:
            logger.info(
                "Keycloak organization lock for %s: the savepoint rollback failed: %s", alias, type(exc).__name__
            )
            return False
        return True

    async def _rollback(self) -> None:
        try:
            await self._session.rollback()
        except DATABASE_FAILURES as exc:
            # The failure that called for the rollback is what the push reports.
            logger.warning("Keycloak link: the rollback failed too: %s", type(exc).__name__)

    async def _discard_connection(self) -> None:
        """Invalidate the session's connection: the server ends its transaction, and the lock with it."""
        try:
            with anyio.fail_after(ORGANIZATION_LOCK_RELEASE_SECONDS):
                await self._session.invalidate()
        except DATABASE_FAILURES as exc:  # TimeoutError (fail_after) is an OSError
            logger.warning("Keycloak link: invalidating the connection failed: %s", type(exc).__name__)


def _put_back(loaded: Loaded) -> None:
    """Put each row's loaded values back as committed, which a rollback expired."""
    for row, values in loaded:
        for key, value in values.items():
            set_committed_value(row, key, value)


def build_keycloak_sync_adapter(api: KeycloakAdminApi, session: AsyncSession) -> KeycloakSyncAdapter:
    """The adapter for one request: Keycloak through ``api``, the canonical model through ``session``."""
    return KeycloakSyncAdapter(api, RepositoryKeycloakLinks(session))
