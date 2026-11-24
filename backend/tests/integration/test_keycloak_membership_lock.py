"""The per-membership Keycloak push lock against the test Postgres: taken from the session's own engine pool."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.keycloak_membership_lock import hold_keycloak_membership_lock


async def test_the_lock_comes_from_the_sessions_engine_and_excludes_another_holder(db_session, async_engine):
    user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    lock = hold_keycloak_membership_lock(db_session, user_id, tenant_id)

    async with lock as held:
        assert held is True
        # No engine of its own: the pool and options are the app's.
        assert lock._engine.sync_engine is db_session.get_bind().engine
        async with async_sessionmaker(async_engine, class_=AsyncSession)() as other:
            async with hold_keycloak_membership_lock(other, user_id, tenant_id, wait_seconds=0.2) as also_held:
                assert also_held is False

    async with async_sessionmaker(async_engine, class_=AsyncSession)() as other:
        async with hold_keycloak_membership_lock(other, user_id, tenant_id, wait_seconds=0.2) as held_after:
            assert held_after is True
