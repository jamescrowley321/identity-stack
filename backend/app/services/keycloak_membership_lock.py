"""Shared membership lock for Keycloak membership pushes.

Guards concurrent pushes of the same (user, tenant) membership so an inline
push cannot race with a reconciliation push and clear the pending mark first.
"""

from __future__ import annotations

import asyncio
import uuid

from opentelemetry import trace
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

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
        self._close_connection = False
        self._dispose_engine = False

    async def __aenter__(self) -> bool:
        try:
            bind = self._session.get_bind()
            if isinstance(bind, AsyncConnection):
                engine = bind.engine
                self._dispose_engine = False
            elif isinstance(bind, AsyncEngine):
                engine = bind
                self._dispose_engine = False
            else:
                url = getattr(bind, "url", None)
                if url is None and hasattr(bind, "engine"):
                    url = getattr(bind.engine, "url", None)
                if url is None:
                    raise RuntimeError("Keycloak membership lock requires a database URL")
                rendered = url.render_as_string(hide_password=False) if hasattr(url, "render_as_string") else str(url)
                engine = create_async_engine(rendered)
                self._dispose_engine = True
            self._engine = engine
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            self._lock_session = factory()
            self._connection = await self._lock_session.connection()
            self._close_connection = True
        except Exception as exc:  # pragma: no cover - defensive logging only
            logger.warning("Keycloak membership lock skipped: %s", type(exc).__name__)
            self._lock_session = None
            self._connection = None
            self._close_connection = False
            self._engine = None
            self._dispose_engine = False
            self._taken = False
            return False
        deadline = asyncio.get_running_loop().time() + self._wait_seconds
        while True:
            try:
                self._taken = await _try_lock(self._connection, self._member_key)
            except Exception as exc:  # pragma: no cover - defensive logging only
                logger.warning(
                    "Keycloak membership lock acquisition failed; dropping connection: %s", type(exc).__name__
                )
                await self._connection.invalidate()
                self._taken = False
                break
            if self._taken or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(_LOCK_POLL_SECONDS)
        return self._taken

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._connection is None:
            return
        try:
            if self._taken:
                await _unlock(self._connection, self._member_key)
        except Exception as error:  # pragma: no cover - defensive logging only
            logger.warning("Keycloak membership lock release failed; dropping connection: %s", type(error).__name__)
            if self._close_connection and self._connection is not None:
                await self._connection.invalidate()
        finally:
            if self._lock_session is not None:
                await self._lock_session.close()
            elif self._close_connection and self._connection is not None:
                await self._connection.close()
            if self._dispose_engine and self._engine is not None:
                await self._engine.dispose()


def hold_keycloak_membership_lock(
    session: AsyncSession,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    wait_seconds: float = _MEMBERSHIP_LOCK_WAIT_SECONDS,
) -> _MembershipLock:
    with tracer.start_as_current_span("keycloak_membership_lock.hold_keycloak_membership_lock"):
        return _MembershipLock(session, user_id, tenant_id, wait_seconds)
