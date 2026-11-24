"""Shared membership lock for Keycloak membership pushes.

Guards concurrent pushes of the same (user, tenant) membership so an inline
push cannot race with a reconciliation push and clear the pending mark first.
"""

from __future__ import annotations

import asyncio
import uuid

from opentelemetry import trace
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.logbook import logbook

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

# Session-level advisory lock "class" for Keycloak memberships ("KCMB").
_MEMBERSHIP_LOCK_CLASS = 0x4B434D42
_LOCK_POLL_SECONDS = 0.1
# Bound the wait for a held membership lock: another push will retry later.
_MEMBERSHIP_LOCK_WAIT_SECONDS = 2.0


def _member_lock_key(user_id: uuid.UUID, tenant_id: uuid.UUID) -> int:
    """Deterministic 32-bit key for the membership's advisory lock."""

    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"keycloak-membership:{user_id}:{tenant_id}")
    return digest.int & 0x7FFFFFFF


async def _try_lock(connection: AsyncConnection, member_key: int) -> bool:
    return bool(
        (
            await connection.execute(  # type: ignore[arg-type]
                text("SELECT pg_try_advisory_lock(:class_id, :member_id)"),
                {"class_id": _MEMBERSHIP_LOCK_CLASS, "member_id": member_key},
            )
        ).scalar()
    )


async def _unlock(connection: AsyncConnection, member_key: int) -> None:
    await connection.execute(  # type: ignore[arg-type]
        text("SELECT pg_advisory_unlock(:class_id, :member_id)"),
        {"class_id": _MEMBERSHIP_LOCK_CLASS, "member_id": member_key},
    )


class _MembershipLock:
    def __init__(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        wait_seconds: float = _MEMBERSHIP_LOCK_WAIT_SECONDS,
    ) -> None:
        self._session = session
        self._member_key = _member_lock_key(user_id, tenant_id)
        self._wait_seconds = wait_seconds
        self._connection: AsyncConnection | None = None
        self._lock_session: AsyncSession | None = None
        self._engine: AsyncEngine | None = None
        self._taken = False

    async def __aenter__(self) -> bool:
        """Take the lock on a connection of its own from the session's engine pool (never a new engine)."""
        try:
            bind = self._session.get_bind()
            # The sync engine the session's AsyncEngine wraps: its pool and options are the app's.
            self._engine = AsyncEngine(bind.engine if isinstance(bind, Connection) else bind)
            self._lock_session = async_sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)()
            self._connection = await self._lock_session.connection()
        except BaseException as exc:
            await self._drop()
            if not isinstance(exc, Exception):
                raise
            logger.warning("Keycloak membership lock skipped: %s", type(exc).__name__)
            return False
        deadline = asyncio.get_running_loop().time() + self._wait_seconds
        try:
            while True:
                self._taken = await _try_lock(self._connection, self._member_key)
                if self._taken or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(_LOCK_POLL_SECONDS)
        except BaseException as exc:
            # Closing the connection frees a lock Postgres granted before the failure or cancel.
            logger.warning("Keycloak membership lock acquisition failed; dropping connection: %s", type(exc).__name__)
            self._taken = False
            await self._drop(invalidate=True)
            if not isinstance(exc, Exception):
                raise
        return self._taken

    async def _drop(self, *, invalidate: bool = False) -> None:
        """Close the lock's session, dropping its connection (not back to the pool) when ``invalidate``."""
        connection, session = self._connection, self._lock_session
        self._connection = self._lock_session = None
        if invalidate and connection is not None:
            await connection.invalidate()
        if session is not None:
            await session.close()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._connection is None:
            return
        try:
            if self._taken:
                await _unlock(self._connection, self._member_key)
        except BaseException as error:
            logger.warning("Keycloak membership lock release failed; dropping connection: %s", type(error).__name__)
            await self._drop(invalidate=True)
            if not isinstance(error, Exception):
                raise
            return
        await self._drop()


def hold_keycloak_membership_lock(
    session: AsyncSession,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    wait_seconds: float = _MEMBERSHIP_LOCK_WAIT_SECONDS,
) -> _MembershipLock:
    with tracer.start_as_current_span("keycloak_membership_lock.hold_keycloak_membership_lock"):
        return _MembershipLock(session, user_id, tenant_id, wait_seconds)
