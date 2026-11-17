"""The Keycloak organization lock (dependencies/identity.py) against real Postgres.

KeycloakSyncAdapter reads an organization and sends it back whole under this
lock, so two such writes to one organization from any worker never interleave.
The lock is taken on the request's session connection: a request never needs a
second pooled connection to take it.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.dependencies.identity import keycloak_organization_lock
from app.services.adapters.keycloak import LocalDatabaseError

pytestmark = pytest.mark.asyncio

# Long enough for a holder that did not wait on the lock to have run.
SETTLE_SECONDS = 0.3
# A pool this small is exhausted by a few concurrent requests.
POOL_SIZE = 2
# Longer than the concurrent requests below take, far shorter than the test's bound.
POOL_TIMEOUT_SECONDS = 3
# More concurrent membership endings than the pool has connections.
REQUESTS = POOL_SIZE * 3
# Held by each request under the lock, standing in for the Keycloak calls.
KEYCLOAK_SECONDS = 0.05
# How long a push waits for the lock in the slow-holder test: far shorter than the pool's checkout timeout.
LOCK_WAIT_SECONDS = 0.3


def sessions(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def test_a_second_writer_of_the_organization_waits_and_another_organization_does_not(async_engine):
    factory = sessions(async_engine)
    organization, other = f"organization-{uuid.uuid4()}", f"organization-{uuid.uuid4()}"
    order: list[str] = []
    held = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with factory() as session, keycloak_organization_lock(session)(organization):
            order.append("first holds")
            held.set()
            await release.wait()
            order.append("first releases")

    async def second() -> None:
        await held.wait()
        async with factory() as session, keycloak_organization_lock(session)(organization):
            order.append("second holds")

    async def elsewhere() -> None:
        await held.wait()
        async with factory() as session, keycloak_organization_lock(session)(other):
            order.append("other organization holds")

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await elsewhere()
    await asyncio.sleep(SETTLE_SECONDS)
    release.set()
    await asyncio.gather(*tasks)

    assert order == ["first holds", "other organization holds", "first releases", "second holds"]


async def test_the_lock_is_released_when_the_body_raises(async_engine):
    factory = sessions(async_engine)
    organization = f"organization-{uuid.uuid4()}"

    async with factory() as session:
        with pytest.raises(LookupError):
            async with keycloak_organization_lock(session)(organization):
                raise LookupError("the body failed")

        async with asyncio.timeout(5), factory() as other, keycloak_organization_lock(other)(organization):
            pass


async def test_the_lock_is_released_when_left_while_the_requests_session_stays_open(async_engine):
    """A request reads on after the push (restore_memberships): its session outlives the lock."""
    factory = sessions(async_engine)
    organization = f"organization-{uuid.uuid4()}"

    async with factory() as session:
        async with keycloak_organization_lock(session)(organization):
            pass

        async with asyncio.timeout(5), factory() as other, keycloak_organization_lock(other)(organization):
            pass
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_more_concurrent_membership_endings_than_the_pool_holds_neither_deadlock_nor_stall_others(
    postgres_url, _run_migrations
):
    """Each request holds its session's connection (the IdP link read) when it takes the lock, as the adapter does."""
    engine = create_async_engine(postgres_url, pool_size=POOL_SIZE, max_overflow=0, pool_timeout=POOL_TIMEOUT_SECONDS)
    factory = sessions(engine)
    organization = f"organization-{uuid.uuid4()}"
    ended: list[int] = []

    async def end_membership(request: int) -> None:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
            async with keycloak_organization_lock(session)(organization):
                await asyncio.sleep(KEYCLOAK_SECONDS)
            await session.execute(text("SELECT 1"))
            ended.append(request)

    async def another_route() -> int:
        async with factory() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one()

    try:
        async with asyncio.timeout(POOL_TIMEOUT_SECONDS * 4):
            endings = [asyncio.create_task(end_membership(request)) for request in range(REQUESTS)]
            answered = await another_route()
            await asyncio.gather(*endings)
    finally:
        await engine.dispose()

    assert (sorted(ended), answered) == (list(range(REQUESTS)), 1)


async def test_requests_behind_a_holder_slower_than_the_pool_timeout_give_up_and_other_routes_answer(
    postgres_url, _run_migrations, monkeypatch
):
    """The holder keeps the lock until another route has answered: without a wait limit that never happens."""
    monkeypatch.setattr("app.dependencies.identity.ORGANIZATION_LOCK_TIMEOUT_MS", round(LOCK_WAIT_SECONDS * 1000))
    engine = create_async_engine(postgres_url, pool_size=POOL_SIZE, max_overflow=0, pool_timeout=POOL_TIMEOUT_SECONDS)
    factory = sessions(engine)
    organization = f"organization-{uuid.uuid4()}"
    held = asyncio.Event()
    answered = asyncio.Event()
    outcomes: dict[int, str] = {}

    async def holder() -> None:
        async with factory() as session, keycloak_organization_lock(session)(organization):
            held.set()
            await answered.wait()

    async def waiter(request: int) -> None:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
            try:
                async with keycloak_organization_lock(session)(organization):
                    outcomes[request] = "held"
            except LocalDatabaseError as exc:
                outcomes[request] = str(exc)

    async def another_route() -> int:
        async with factory() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one()

    try:
        async with asyncio.timeout(POOL_TIMEOUT_SECONDS * 4):
            holding = asyncio.create_task(holder())
            await held.wait()
            waiting = [asyncio.create_task(waiter(request)) for request in range(REQUESTS)]
            await asyncio.sleep(SETTLE_SECONDS)
            try:
                other = await another_route()
            finally:
                answered.set()
                await asyncio.gather(holding, *waiting)
    finally:
        await engine.dispose()

    gave_up = "the organization lock failed in the service's database: DBAPIError"
    assert (other, outcomes) == (1, dict.fromkeys(range(REQUESTS), gave_up))


async def test_the_wait_limit_ends_with_the_lock(async_engine, monkeypatch):
    """The limit is local to the lock: the request's later statements wait as they did."""
    monkeypatch.setattr("app.dependencies.identity.ORGANIZATION_LOCK_TIMEOUT_MS", 2500)
    factory = sessions(async_engine)

    async with factory() as session:
        before = (await session.execute(text("SHOW lock_timeout"))).scalar_one()
        async with keycloak_organization_lock(session)(f"organization-{uuid.uuid4()}"):
            during = (await session.execute(text("SHOW lock_timeout"))).scalar_one()
        after = (await session.execute(text("SHOW lock_timeout"))).scalar_one()

    assert (before, during, after) == ("0", "2500ms", "0")


async def test_a_database_failure_taking_the_lock_is_a_local_database_error(postgres_url, _run_migrations):
    engine = create_async_engine(postgres_url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    factory = sessions(engine)
    try:
        async with engine.connect() as holder:
            await holder.execute(text("SELECT 1"))
            async with factory() as session:
                with pytest.raises(LocalDatabaseError):
                    async with keycloak_organization_lock(session)(f"organization-{uuid.uuid4()}"):
                        pass
    finally:
        await engine.dispose()
