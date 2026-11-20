"""Data access for reconciliation: the pending-push ledger, each provider's last pass, and bulk canonical reads.

Contains NO business logic, NO OTel spans — data access only.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Permission, Role, RolePermission
from app.models.identity.sync_event import SyncEvent, SyncEventVerb
from app.models.identity.sync_ledger import PendingPush, ReconciliationRun
from app.models.identity.tenant import Tenant
from app.models.identity.user import IdPLink, User
from app.repositories.base import BaseRepository

# A mark whose push keeps failing waits ``interval * 2 ** n`` before the next retry, n at most this.
MAX_BACKOFF_DOUBLINGS = 6

# The transaction setting a canonical write that brings the canonical model to the
# provider's state sets (migration 009): such a write marks nothing pending.
SYNC_ORIGIN_SETTING = "identity_stack.sync_origin"
PROVIDER_ORIGIN = "provider"


class PendingPushRepository(BaseRepository[PendingPush]):
    """The canonical entities changed since a reconciliation pass last confirmed them in the push provider."""

    _model = PendingPush

    async def list_all(self) -> list[PendingPush]:
        """Every mark as committed now (never a copy the session already holds)."""
        stmt = (
            sa.select(PendingPush).order_by(PendingPush.kind, PendingPush.key).execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_due(self, interval_seconds: float) -> list[PendingPush]:
        """The marks a push retry pushes now: marked at least ``interval_seconds`` ago, and backed off.

        A mark is backed off by the ``failed`` sync events of its entity since it was
        marked: after n of them the next retry waits ``interval * 2 ** (n - 1)`` from
        the last, at most ``interval * 2 ** MAX_BACKOFF_DOUBLINGS``. Read from the
        database, so every worker sees the same cadence per mark.
        """
        failures = (
            sa.select(
                PendingPush.kind,
                PendingPush.key,
                sa.func.count().label("failures"),
                sa.func.max(SyncEvent.occurred_at).label("last_failure"),
            )
            .join(
                SyncEvent,
                sa.and_(
                    SyncEvent.subject_type == PendingPush.kind,
                    SyncEvent.subject_id == PendingPush.key,
                    SyncEvent.occurred_at >= PendingPush.marked_at,
                ),
            )
            .where(SyncEvent.verb == SyncEventVerb.failed)
            .group_by(PendingPush.kind, PendingPush.key)
            .subquery()
        )
        now = sa.func.clock_timestamp()
        backoff = interval_seconds * sa.func.power(2, sa.func.least(failures.c.failures - 1, MAX_BACKOFF_DOUBLINGS))
        stmt = (
            sa.select(PendingPush)
            .outerjoin(failures, sa.and_(failures.c.kind == PendingPush.kind, failures.c.key == PendingPush.key))
            .where(
                PendingPush.marked_at <= now - sa.func.make_interval(0, 0, 0, 0, 0, 0, interval_seconds),
                sa.or_(
                    failures.c.failures.is_(None),
                    failures.c.last_failure <= now - sa.func.make_interval(0, 0, 0, 0, 0, 0, backoff),
                ),
            )
            .order_by(PendingPush.kind, PendingPush.key)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def seqs(self, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], int]:
        """The ``seq`` of each of ``keys`` that is marked now."""
        wanted = set(keys)
        if not wanted:
            return {}
        stmt = sa.select(PendingPush.kind, PendingPush.key, PendingPush.seq).where(
            sa.tuple_(PendingPush.kind, PendingPush.key).in_(list(wanted))
        )
        return {(kind, key): seq for kind, key, seq in (await self._session.execute(stmt)).all()}

    async def clear(self, kind: str, key: str, seq: int) -> bool:
        """Remove the mark, unless a change after ``seq`` marked it again."""
        stmt = sa.delete(PendingPush).where(PendingPush.kind == kind, PendingPush.key == key, PendingPush.seq <= seq)
        result = await self._session.execute(stmt)
        return result.rowcount > 0

    async def count(self) -> int:
        return int((await self._session.execute(sa.select(sa.func.count()).select_from(PendingPush))).scalar() or 0)

    async def provider_writes(self, on: bool) -> None:
        """Mark the rest of the transaction's canonical writes as the provider's state (``on``), or not."""
        await self._session.execute(
            sa.select(sa.func.set_config(SYNC_ORIGIN_SETTING, PROVIDER_ORIGIN if on else "", True))
        )


class ReconciliationRunRepository(BaseRepository[ReconciliationRun]):
    """Each provider's last reconciliation pass."""

    _model = ReconciliationRun

    async def record(self, provider: str, *, status: str, stats: dict, error: str | None) -> None:
        values = {"provider": provider, "status": status, "stats": stats, "error": error, "finished_at": sa.func.now()}
        stmt = insert(ReconciliationRun).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ReconciliationRun.provider],
            set_={key: stmt.excluded[key] for key in ("status", "stats", "error", "finished_at")},
        )
        await self._session.execute(stmt)

    async def list_all(self) -> list[ReconciliationRun]:
        stmt = sa.select(ReconciliationRun).order_by(ReconciliationRun.provider)
        return list((await self._session.execute(stmt)).scalars().all())


class CanonicalStateRepository:
    """Whole-table reads of the canonical model, for comparing it with a provider in one pass."""

    def __init__(self, session) -> None:
        self._session = session

    async def _all(self, stmt) -> list:
        return list((await self._session.execute(stmt.execution_options(populate_existing=True))).scalars().all())

    async def tenants_linked_to(self, provider_id: uuid.UUID) -> list[Tenant]:
        return await self._all(
            sa.select(Tenant)
            .where(Tenant.external_org_provider_id == provider_id, Tenant.external_org_id.is_not(None))
            .order_by(Tenant.id)
        )

    async def links_of(self, provider_id: uuid.UUID) -> list[IdPLink]:
        return await self._all(sa.select(IdPLink).where(IdPLink.provider_id == provider_id).order_by(IdPLink.id))

    async def users(self, user_ids: Iterable[uuid.UUID]) -> list[User]:
        wanted = set(user_ids)
        if not wanted:
            return []
        return await self._all(sa.select(User).where(User.id.in_(wanted)).order_by(User.id))

    async def tenants(self, tenant_ids: Iterable[uuid.UUID]) -> list[Tenant]:
        wanted = set(tenant_ids)
        if not wanted:
            return []
        return await self._all(sa.select(Tenant).where(Tenant.id.in_(wanted)).order_by(Tenant.id))

    async def roles(self) -> list[Role]:
        return await self._all(sa.select(Role).order_by(Role.id))

    async def permissions(self) -> list[Permission]:
        return await self._all(sa.select(Permission).order_by(Permission.id))

    async def role_permissions(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """Every (role id, permission id) pair."""
        stmt = sa.select(RolePermission.role_id, RolePermission.permission_id).order_by(
            RolePermission.role_id, RolePermission.permission_id
        )
        return [(role_id, permission_id) for role_id, permission_id in (await self._session.execute(stmt)).all()]

    async def assignments_in(self, tenant_ids: Iterable[uuid.UUID]) -> list[UserTenantRole]:
        wanted = set(tenant_ids)
        if not wanted:
            return []
        return await self._all(
            sa.select(UserTenantRole)
            .where(UserTenantRole.tenant_id.in_(wanted))
            .order_by(UserTenantRole.tenant_id, UserTenantRole.user_id, UserTenantRole.role_id)
        )

    async def assignments_of(self, user_id: uuid.UUID, tenant_id: uuid.UUID) -> list[UserTenantRole]:
        return await self._all(
            sa.select(UserTenantRole)
            .where(UserTenantRole.user_id == user_id, UserTenantRole.tenant_id == tenant_id)
            .order_by(UserTenantRole.role_id)
        )
