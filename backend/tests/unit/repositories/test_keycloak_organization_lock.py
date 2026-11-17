"""The Keycloak organization lock (dependencies/identity.py) against real Postgres.

KeycloakSyncAdapter reads an organization and sends it back whole under this
lock, so two such writes to one organization from any worker never interleave.
"""

import asyncio
import uuid

import pytest

from app.dependencies.identity import keycloak_organization_lock

pytestmark = pytest.mark.asyncio

# Long enough for a holder that did not wait on the lock to have run.
SETTLE_SECONDS = 0.3


async def test_a_second_writer_of_the_organization_waits_and_another_organization_does_not(async_engine):
    lock = keycloak_organization_lock(lambda: async_engine)
    organization, other = f"organization-{uuid.uuid4()}", f"organization-{uuid.uuid4()}"
    order: list[str] = []
    held = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with lock(organization):
            order.append("first holds")
            held.set()
            await release.wait()
            order.append("first releases")

    async def second() -> None:
        await held.wait()
        async with lock(organization):
            order.append("second holds")

    async def elsewhere() -> None:
        await held.wait()
        async with lock(other):
            order.append("other organization holds")

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await elsewhere()
    await asyncio.sleep(SETTLE_SECONDS)
    release.set()
    await asyncio.gather(*tasks)

    assert order == ["first holds", "other organization holds", "first releases", "second holds"]


async def test_the_lock_is_released_when_the_body_raises(async_engine):
    lock = keycloak_organization_lock(lambda: async_engine)
    organization = f"organization-{uuid.uuid4()}"

    with pytest.raises(LookupError):
        async with lock(organization):
            raise LookupError("the body failed")

    async with asyncio.timeout(5):
        async with lock(organization):
            pass
