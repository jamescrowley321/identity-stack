"""KeycloakPullStateRepository — the Keycloak event pull's state row and its lock.

Contains NO business logic, NO OTel spans — data access only.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.models.identity.keycloak_pull import KeycloakPullState
from app.repositories.base import BaseRepository

# Postgres advisory lock key of the pull (arbitrary constant): one poll at a time
# across every worker and process that shares the database.
PULL_LOCK_ID = 75_67_80_85_76  # "KCPUL" in digits


class KeycloakPullStateRepository(BaseRepository[KeycloakPullState]):
    _model = KeycloakPullState

    async def try_lock(self) -> bool:
        """Take the pull's advisory lock until the transaction ends; ``False`` when another session holds it."""
        result = await self._session.execute(sa.select(sa.func.pg_try_advisory_xact_lock(PULL_LOCK_ID)))
        return bool(result.scalar())

    async def get_realm(self, realm: str) -> KeycloakPullState | None:
        """The realm's state as committed (never a stale copy the session already holds)."""
        stmt = (
            sa.select(KeycloakPullState)
            .where(KeycloakPullState.realm == realm)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def start(
        self,
        realm: str,
        *,
        admin: tuple[int, dict[str, int]],
        user: tuple[int, dict[str, int]],
        organizations: dict,
        client_roles: dict,
    ) -> KeycloakPullState:
        """Store a realm's first state: each stream's cursor and the ids already at it, and the snapshot."""
        return await self.create(
            KeycloakPullState(
                realm=realm,
                admin_cursor=admin[0],
                admin_seen=admin[1],
                user_cursor=user[0],
                user_seen=user[1],
                organizations=organizations,
                client_roles=client_roles,
            )
        )
