"""A Keycloak push whose own database work fails, on a real request session (Postgres).

The push runs after the service committed, then the service reads the rows it
loaded before the push. A failed IdP link or tenant link read must leave
those rows loaded and the session usable: the committed change answers with
``provider_sync: failed`` (or logs), it does not raise.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import anyio
import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine

from app.dependencies import keycloak_sync
from app.dependencies.keycloak_sync import ORGANIZATION_LOCK_NAMESPACE, RepositoryKeycloakLinks, UncommittedWorkLost
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import IdPLink, User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.adapters import keycloak as keycloak_adapter
from app.services.adapters.base import PROVIDER_SYNC, PROVIDER_SYNC_FAILED
from app.services.adapters.keycloak import PROVIDER_NAME, KeycloakSyncAdapter, LinkRefused, LocalDatabaseError
from app.services.memberships import restore_memberships
from app.services.user import UserService

pytestmark = pytest.mark.asyncio


def sessions(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def failing_read(self, user_id: uuid.UUID, provider_name: str, *origins) -> str | None:
    """A read the database refuses: the request's transaction is left aborted, as a real failure leaves it."""
    await self._session.execute(text("SELECT * FROM no_such_table"))
    return None


def adapter(session: AsyncSession) -> KeycloakSyncAdapter:
    api = AsyncMock()
    api.find_organization.return_value = {"id": "organization-id"}
    return KeycloakSyncAdapter(api, RepositoryKeycloakLinks(session))


@pytest.fixture
async def members(async_engine):
    """Two users holding a role in one tenant, committed; removed afterwards."""
    tenant = Tenant(id=uuid.uuid4(), name=f"tenant-{uuid.uuid4().hex[:8]}")
    role = Role(id=uuid.uuid4(), name=f"role-{uuid.uuid4().hex[:8]}", tenant_id=tenant.id)
    users = [
        User(
            id=uuid.uuid4(),
            email=f"member-{uuid.uuid4().hex[:8]}@test.com",
            user_name="member",
            status=UserStatus.active,
        )
        for _ in range(2)
    ]
    async with sessions(async_engine)() as setup:
        setup.add_all([tenant, *users])
        await setup.flush()
        setup.add(role)
        await setup.flush()
        setup.add_all([UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id) for user in users])
        await setup.commit()
    yield tenant, role, users
    async with sessions(async_engine)() as cleanup:
        await cleanup.execute(delete(UserTenantRole).where(UserTenantRole.tenant_id == tenant.id))
        await cleanup.execute(delete(Role).where(Role.id == role.id))
        await cleanup.execute(delete(User).where(User.id.in_([user.id for user in users])))
        await cleanup.execute(delete(Tenant).where(Tenant.id == tenant.id))
        await cleanup.commit()


async def test_a_deactivation_whose_idp_link_read_fails_answers_provider_sync_failed(
    async_engine, members, monkeypatch
):
    tenant, _role, users = members
    user = users[0]
    monkeypatch.setattr(IdPLinkRepository, "sub_made_by", failing_read)

    async with sessions(async_engine)() as session:
        service = UserService(repository=UserRepository(session), adapter=adapter(session), publisher=AsyncMock())
        result = await service.deactivate_user(tenant_id=tenant.id, user_id=user.id)
        still_answers = (await session.execute(text("SELECT 1"))).scalar_one()

    assert result.is_ok()
    assert (result.ok["id"], result.ok["status"], result.ok[PROVIDER_SYNC], still_answers) == (
        user.id,
        UserStatus.inactive,
        PROVIDER_SYNC_FAILED,
        1,
    )


async def test_restoring_memberships_whose_idp_link_reads_fail_logs_each_and_does_not_raise(
    async_engine, members, monkeypatch, caplog
):
    tenant, role, users = members
    monkeypatch.setattr(IdPLinkRepository, "sub_made_by", failing_read)
    caplog.set_level(logging.WARNING, logger="app.services.memberships")

    async with sessions(async_engine)() as session:
        await restore_memberships(
            UserTenantRoleRepository(session), adapter(session), [(user.id, tenant.id) for user in users]
        )

    logged = [record.getMessage() for record in caplog.records if record.name == "app.services.memberships"]
    expected = (
        f"IdP sync failed for role {role.id} (restore_membership): "
        "the IdP link read failed in the service's database: ProgrammingError"
    )
    assert logged == [expected, expected]


async def failing_tenant_read(self, tenant_id: uuid.UUID) -> Tenant | None:
    await self._session.execute(text("SELECT * FROM no_such_table"))
    return None


async def test_a_tenant_link_read_that_fails_is_a_sync_error_and_leaves_the_requests_rows_loaded(
    async_engine, members, monkeypatch
):
    _tenant, _role, users = members
    monkeypatch.setattr(TenantRepository, "get", failing_tenant_read)
    tenant_id = uuid.uuid4()

    async with sessions(async_engine)() as session:
        user = await UserRepository(session).get(users[0].id)
        await session.commit()
        result = await adapter(session).sync_tenant(tenant_id=tenant_id, data={"name": "organization"})
        read_after = (user.email, (await session.execute(text("SELECT 1"))).scalar_one())

    assert result.is_error()
    assert (result.error.message, result.error.context, read_after) == (
        "the tenant link read failed in the service's database: ProgrammingError",
        {"tenant_id": str(tenant_id), "status": None},
        (users[0].email, 1),
    )


@pytest.fixture
async def keycloak_provider(async_engine):
    """The ``keycloak`` provider row, created for the test when the database has none; removed if created."""
    async with sessions(async_engine)() as setup:
        existing = await ProviderRepository(setup).get_by_name(PROVIDER_NAME)
        if existing is not None:
            yield existing
            return
        provider = Provider(name=PROVIDER_NAME, type=ProviderType.oidc)
        setup.add(provider)
        await setup.commit()
    yield provider
    async with sessions(async_engine)() as cleanup:
        await cleanup.execute(delete(IdPLink).where(IdPLink.provider_id == provider.id))
        await cleanup.execute(delete(Provider).where(Provider.id == provider.id))
        await cleanup.commit()


async def test_a_link_a_sign_in_wrote_meanwhile_counts_as_written_or_is_refused(
    async_engine, members, keycloak_provider, monkeypatch
):
    """The insert loses a race with a sign-in: the same link is written, another one refuses the push."""
    _tenant, _role, users = members
    first, second = users
    sub = f"keycloak-{uuid.uuid4()}"
    async with sessions(async_engine)() as setup:
        setup.add(IdPLink(user_id=first.id, provider_id=keycloak_provider.id, external_sub=sub, external_email="x"))
        await setup.commit()
    real = IdPLinkRepository.get_by_provider_and_sub
    asked: list[str] = []

    async def missed_once(self, provider_id: uuid.UUID, external_sub: str) -> IdPLink | None:
        # The check before the insert does not see the sign-in's link yet.
        asked.append(external_sub)
        return None if len(asked) == 1 else await real(self, provider_id, external_sub)

    try:
        monkeypatch.setattr(IdPLinkRepository, "get_by_provider_and_sub", missed_once)
        async with sessions(async_engine)() as session:
            await RepositoryKeycloakLinks(session).link_user(first.id, sub, "x")
        asked.clear()
        async with sessions(async_engine)() as session:
            with pytest.raises(LinkRefused):
                await RepositoryKeycloakLinks(session).link_user(second.id, sub, "x")
    finally:
        async with sessions(async_engine)() as cleanup:
            await cleanup.execute(delete(IdPLink).where(IdPLink.external_sub == sub))
            await cleanup.commit()


async def dropping_read(self, user_id: uuid.UUID, provider_name: str, *origins) -> str | None:
    """The request's connection drops under the read: Postgres ends its backend, as a failover or reset does."""
    await self._session.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
    return None


CONNECTION_LOST = "the IdP link read failed in the service's database: the connection was lost (DBAPIError)"


async def test_restoring_memberships_whose_connection_drops_logs_each_and_leaves_the_session_usable(
    async_engine, members, monkeypatch, caplog
):
    tenant, role, users = members
    monkeypatch.setattr(IdPLinkRepository, "sub_made_by", dropping_read)
    caplog.set_level(logging.WARNING, logger="app.services.memberships")

    async with sessions(async_engine)() as session:
        await restore_memberships(
            UserTenantRoleRepository(session), adapter(session), [(user.id, tenant.id) for user in users]
        )
        still_answers = (await session.execute(text("SELECT 1"))).scalar_one()

    logged = [record.getMessage() for record in caplog.records if record.name == "app.services.memberships"]
    expected = f"IdP sync failed for role {role.id} (restore_membership): {CONNECTION_LOST}"
    assert (logged, still_answers) == ([expected, expected], 1)


async def test_a_deactivation_whose_connection_drops_answers_provider_sync_failed(async_engine, members, monkeypatch):
    tenant, _role, users = members
    user = users[0]
    monkeypatch.setattr(IdPLinkRepository, "sub_made_by", dropping_read)

    async with sessions(async_engine)() as session:
        service = UserService(repository=UserRepository(session), adapter=adapter(session), publisher=AsyncMock())
        result = await service.deactivate_user(tenant_id=tenant.id, user_id=user.id)
        still_answers = (await session.execute(text("SELECT 1"))).scalar_one()

    assert result.is_ok()
    assert (result.ok["id"], result.ok["status"], result.ok[PROVIDER_SYNC], still_answers) == (
        user.id,
        UserStatus.inactive,
        PROVIDER_SYNC_FAILED,
        1,
    )


async def test_a_lost_connection_under_an_unflushed_change_rolls_it_back_and_says_so(
    async_engine, members, monkeypatch
):
    tenant, _role, users = members
    monkeypatch.setattr(IdPLinkRepository, "sub_made_by", dropping_read)

    async with sessions(async_engine)() as session:
        row = await TenantRepository(session).get(tenant.id)
        row.name = "renamed, not committed"
        with pytest.raises(UncommittedWorkLost) as lost:
            await RepositoryKeycloakLinks(session).linked_subject(users[0].id)
        await session.commit()
    async with sessions(async_engine)() as check:
        held = (await TenantRepository(check).get(tenant.id)).name

    assert (str(lost.value), held) == (
        f"{CONNECTION_LOST}; the request's uncommitted changes were rolled back",
        tenant.name,
    )


@pytest.mark.parametrize("flushed", [False, True], ids=["pending", "flushed"])
async def test_a_lost_connection_under_an_uncommitted_row_rolls_it_back_and_says_so(
    async_engine, members, monkeypatch, flushed
):
    _tenant, _role, users = members
    monkeypatch.setattr(IdPLinkRepository, "sub_made_by", dropping_read)
    added = Tenant(id=uuid.uuid4(), name=f"tenant-{uuid.uuid4().hex[:8]}")

    async with sessions(async_engine)() as session:
        session.add(added)
        if flushed:
            await session.flush()
        with pytest.raises(UncommittedWorkLost):
            await RepositoryKeycloakLinks(session).linked_subject(users[0].id)
        await session.commit()
    async with sessions(async_engine)() as check:
        held = await TenantRepository(check).get(added.id)

    assert held is None


async def dropping_update(self, entity):
    await self._session.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
    return entity


async def test_a_link_write_that_loses_the_connection_leaves_the_requests_rows_as_committed(
    async_engine, members, keycloak_provider, monkeypatch
):
    tenant, _role, _users = members
    monkeypatch.setattr(TenantRepository, "update", dropping_update)

    async with sessions(async_engine)() as session:
        row = await TenantRepository(session).get(tenant.id)
        with pytest.raises(LocalDatabaseError) as failure:
            await RepositoryKeycloakLinks(session).link_organization(tenant.id, "organization")
        loaded = (row.name, row.external_org_id, row.external_org_provider_id)
        still_answers = (await session.execute(text("SELECT 1"))).scalar_one()

    assert (str(failure.value), loaded, still_answers) == (
        "the tenant link write failed in the service's database: the connection was lost (DBAPIError)",
        (tenant.name, None, None),
        1,
    )


async def test_a_push_whose_pooled_connection_went_stale_after_the_commit_is_a_sync_error(async_engine, members):
    """The push's first statement finds the connection dead (no pre-ping): a sync error, not UncommittedWorkLost."""
    tenant, _role, users = members
    pooled = create_async_engine(async_engine.url, pool_size=1, max_overflow=0)
    try:
        async with sessions(pooled)() as session, async_engine.connect() as other:
            user = await UserRepository(session).get(users[0].id)
            pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await session.commit()
            await other.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            result = await adapter(session).sync_tenant(tenant_id=tenant.id, data={"name": "organization"})
            after = (user.email, (await session.execute(text("SELECT 1"))).scalar_one())
    finally:
        await pooled.dispose()

    assert result.is_error()
    assert result.error.message.startswith(
        "the tenant link read failed in the service's database: "
        f"{keycloak_sync.DatabaseFailure.CONNECTION_LOST.value} ("
    )
    assert result.error.message.endswith(")")
    assert after == (users[0].email, 1)


def tenant_read_running(*statements: str):
    """A tenant read that runs ``statements`` in the request's session (the read's savepoint)."""

    async def read(self, tenant_id: uuid.UUID) -> Tenant | None:
        for statement in statements:
            await self._session.execute(text(statement))
        return None

    return read


HELD_LOCK_KEY = 632_633


@pytest.fixture
async def held_lock(async_engine):
    """An advisory lock another session holds for the test."""
    async with async_engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": HELD_LOCK_KEY})
        yield
        await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": HELD_LOCK_KEY})


@pytest.mark.parametrize(
    ("statements", "named"),
    [
        pytest.param(
            ("SET LOCAL lock_timeout = '50ms'", f"SELECT pg_advisory_xact_lock({HELD_LOCK_KEY})"),
            "a lock was not taken within lock_timeout (DBAPIError)",
            id="lock_timeout",
        ),
        pytest.param(
            ("SET LOCAL statement_timeout = '50ms'", "SELECT pg_sleep(5)"),
            "the statement was canceled (DBAPIError)",
            id="statement_timeout",
        ),
        pytest.param(("SELECT * FROM no_such_table",), "ProgrammingError", id="other"),
    ],
)
async def test_a_tenant_link_read_that_fails_is_named_by_its_sqlstate_and_leaves_the_session_usable(
    async_engine, held_lock, monkeypatch, statements, named
):
    monkeypatch.setattr(TenantRepository, "get", tenant_read_running(*statements))
    tenant_id = uuid.uuid4()

    async with sessions(async_engine)() as session:
        result = await adapter(session).sync_tenant(tenant_id=tenant_id, data={"name": "organization"})
        still_answers = (await session.execute(text("SELECT 1"))).scalar_one()

    assert result.is_error()
    assert (result.error.message, still_answers) == (
        f"the tenant link read failed in the service's database: {named}",
        1,
    )


async def test_a_probe_failure_before_the_read_leaves_the_session_usable(async_engine, held_lock, monkeypatch):
    async def probe(self) -> bool:
        await self._session.execute(text("SET LOCAL lock_timeout = '50ms'"))
        await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": HELD_LOCK_KEY})
        return False

    monkeypatch.setattr(ProviderRepository, "transaction_has_written", probe)
    tenant_id = uuid.uuid4()

    async with sessions(async_engine)() as session:
        # Open a transaction without writes: ``_loaded`` asks whether it has written.
        await session.execute(text("SELECT 1"))
        links = RepositoryKeycloakLinks(session)
        with pytest.raises(LocalDatabaseError):
            await links.organization_alias(tenant_id)
        still_answers = (await session.execute(text("SELECT 1"))).scalar_one()

    assert still_answers == 1


# --- the organization lock (a push that reads an organization and sends it back) ---


@asynccontextmanager
async def organization_held(async_engine, tenant: Tenant) -> AsyncIterator[AsyncConnection]:
    """The lock of the tenant's organization (its alias: the tenant id), held by another connection."""
    async with async_engine.connect() as holder, holder.begin():
        statement = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
        await holder.execute(statement, {"key": ORGANIZATION_LOCK_NAMESPACE + str(tenant.id)})
        yield holder


async def _waiting_for(connection, tenant: Tenant) -> int:
    """The backend that waits on the tenant's organization lock, once one does."""
    statement = text(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        " AND ((classid::bigint << 32) | objid::bigint) = hashtextextended(:key, 0)"
    )
    with anyio.fail_after(10):
        while (
            pid := (await connection.execute(statement, {"key": ORGANIZATION_LOCK_NAMESPACE + str(tenant.id)})).scalar()
        ) is None:
            await asyncio.sleep(0.02)
    return pid


def _lock_warnings(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "app.dependencies.keycloak_sync"]


async def test_a_connection_lost_waiting_on_the_organization_lock_is_a_sync_error_and_the_session_stays_usable(
    async_engine, members, caplog
):
    tenant, _role, users = members
    caplog.set_level(logging.WARNING, logger="app.dependencies.keycloak_sync")

    async with organization_held(async_engine, tenant) as holder, sessions(async_engine)() as session:
        user = await UserRepository(session).get(users[0].id)
        await session.commit()

        async def kill_the_waiter() -> None:
            pid = await _waiting_for(holder, tenant)
            await holder.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

        result, _ = await asyncio.gather(
            adapter(session).sync_tenant(tenant_id=tenant.id, data={"name": "organization"}), kill_the_waiter()
        )
        after = (user.email, (await session.execute(text("SELECT 1"))).scalar_one())

    assert result.is_error()
    assert (result.error.message, after, _lock_warnings(caplog)) == (
        "the organization lock failed in the service's database: the connection was lost (DBAPIError)",
        (users[0].email, 1),
        [],
    )


async def test_an_organization_lock_wait_that_runs_out_is_named_a_lock_timeout(
    async_engine, members, monkeypatch, caplog
):
    tenant, _role, _users = members
    monkeypatch.setattr(keycloak_sync, "ORGANIZATION_LOCK_TIMEOUT_MS", 100)
    caplog.set_level(logging.WARNING, logger="app.dependencies.keycloak_sync")

    async with organization_held(async_engine, tenant), sessions(async_engine)() as session:
        result = await adapter(session).sync_tenant(tenant_id=tenant.id, data={"name": "organization"})
        still_answers = (await session.execute(text("SELECT 1"))).scalar_one()

    assert result.is_error()
    assert (result.error.message, still_answers, _lock_warnings(caplog)) == (
        "the organization lock failed in the service's database: a lock was not taken within lock_timeout (DBAPIError)",
        1,
        [],
    )


async def test_a_push_cancelled_waiting_on_the_organization_lock_leaves_the_session_usable(async_engine, members):
    tenant, _role, users = members

    async with organization_held(async_engine, tenant), sessions(async_engine)() as session:
        user = await UserRepository(session).get(users[0].id)
        await session.commit()
        with anyio.move_on_after(0.3) as waited:
            await adapter(session).sync_tenant(tenant_id=tenant.id, data={"name": "organization"})
        after = (user.email, (await session.execute(text("SELECT 1"))).scalar_one())

    assert (waited.cancelled_caught, after) == (True, (users[0].email, 1))


async def test_keycloak_calls_past_the_deadline_under_the_lock_leave_the_session_usable(
    async_engine, members, monkeypatch
):
    tenant, _role, users = members
    monkeypatch.setattr(keycloak_adapter, "ORGANIZATION_HOLD_SECONDS", 0.1)

    async def slow_find(alias: str) -> dict:
        await anyio.sleep(5)
        return {"id": "organization-id"}

    async with sessions(async_engine)() as session:
        user = await UserRepository(session).get(users[0].id)
        await session.commit()
        api = AsyncMock()
        api.find_organization.side_effect = slow_find
        result = await KeycloakSyncAdapter(api, RepositoryKeycloakLinks(session)).sync_tenant(
            tenant_id=tenant.id, data={"name": "organization"}
        )
        after = (user.email, (await session.execute(text("SELECT 1"))).scalar_one())

    assert result.is_error()
    assert (result.error.context["status"], after) == (None, (users[0].email, 1))


async def test_uncommitted_work_lost_releasing_the_organization_lock_raises_and_restores_rows(
    async_engine, members, monkeypatch
):
    tenant, _role, _users = members

    async def savepoint_never_rolls_back(self, savepoint, alias: str) -> bool:
        return False

    monkeypatch.setattr(RepositoryKeycloakLinks, "_savepoint_rolled_back", savepoint_never_rolls_back)
    alias = str(tenant.id)

    async with sessions(async_engine)() as session:
        row = await TenantRepository(session).get(tenant.id)
        original_name = row.name
        row.name = "renamed, not committed"
        links = RepositoryKeycloakLinks(session)
        with pytest.raises(UncommittedWorkLost) as lost:
            async with links.organization_lock(alias):
                pass
        await session.commit()
    async with sessions(async_engine)() as check:
        held = await TenantRepository(check).get(tenant.id)

    assert (held.name, str(lost.value)) == (
        original_name,
        f"Keycloak organization lock for {alias}: the request's uncommitted changes were rolled back",
    )


async def test_uncommitted_work_lost_releasing_the_organization_lock_outranks_the_body_error(
    async_engine, members, monkeypatch
):
    tenant, _role, _users = members

    async def savepoint_never_rolls_back(self, savepoint, alias: str) -> bool:
        return False

    monkeypatch.setattr(RepositoryKeycloakLinks, "_savepoint_rolled_back", savepoint_never_rolls_back)

    async with sessions(async_engine)() as session:
        row = await TenantRepository(session).get(tenant.id)
        row.name = "renamed, not committed"
        links = RepositoryKeycloakLinks(session)
        with pytest.raises(UncommittedWorkLost) as lost:
            async with links.organization_lock(str(tenant.id)):
                raise LocalDatabaseError("the push failed under the lock")

    assert isinstance(lost.value.__context__, LocalDatabaseError)


async def _try_organization_lock(async_engine, alias: str) -> bool:
    """Whether another connection can take the organization's lock now (held until its transaction ends)."""
    async with async_engine.connect() as other, other.begin():
        statement = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))")
        return (await other.execute(statement, {"key": ORGANIZATION_LOCK_NAMESPACE + alias})).scalar_one()


async def test_releasing_the_organization_lock_is_bounded_and_releases_the_lock_without_uncommitted_work(
    async_engine, members, monkeypatch
):
    tenant, _role, users = members

    async def never_rollback(self, savepoint, alias: str) -> bool:
        await anyio.sleep_forever()
        return False

    monkeypatch.setattr(RepositoryKeycloakLinks, "_savepoint_rolled_back", never_rollback)
    monkeypatch.setattr(keycloak_sync, "ORGANIZATION_LOCK_RELEASE_SECONDS", 0.1)
    alias = str(tenant.id)

    async with sessions(async_engine)() as session:
        await UserRepository(session).get(users[0].id)
        await session.commit()
        links = RepositoryKeycloakLinks(session)
        async with links.organization_lock(alias):
            # Nothing uncommitted to lose: a release that times out is not the push's failure.
            pass
    # After the release timeout, another connection can take the lock.
    assert await _try_organization_lock(async_engine, alias)


async def test_releasing_the_organization_lock_is_bounded_and_releases_the_lock_with_uncommitted_work(
    async_engine, members, monkeypatch
):
    tenant, _role, _users = members

    async def never_rollback(self, savepoint, alias: str) -> bool:
        await anyio.sleep_forever()
        return False

    monkeypatch.setattr(RepositoryKeycloakLinks, "_savepoint_rolled_back", never_rollback)
    monkeypatch.setattr(keycloak_sync, "ORGANIZATION_LOCK_RELEASE_SECONDS", 0.1)
    alias = str(tenant.id)

    async with sessions(async_engine)() as session:
        row = await TenantRepository(session).get(tenant.id)
        original_name = row.name
        row.name = "renamed, not committed"
        links = RepositoryKeycloakLinks(session)
        with pytest.raises(UncommittedWorkLost):
            async with links.organization_lock(alias):
                # Normal exit with uncommitted work: release times out and fails closed.
                pass
        await session.commit()
    async with sessions(async_engine)() as check:
        held = await TenantRepository(check).get(tenant.id)

    assert (held.name, await _try_organization_lock(async_engine, alias)) == (original_name, True)


class BodyError(Exception):
    """Sentinel error for organization_lock bodies in release tests."""


async def test_a_release_whose_rollback_fails_invalidates_the_connection_without_failing_the_push(
    async_engine, members, monkeypatch
):
    tenant, _role, users = members
    alias = str(tenant.id)

    async def savepoint_fails(self, savepoint, alias: str) -> bool:
        return False

    async def failing_rollback(self: AsyncSession) -> None:
        raise SQLAlchemyError("rollback failed")

    monkeypatch.setattr(RepositoryKeycloakLinks, "_savepoint_rolled_back", savepoint_fails)
    monkeypatch.setattr(AsyncSession, "rollback", failing_rollback)
    monkeypatch.setattr(keycloak_sync, "ORGANIZATION_LOCK_RELEASE_SECONDS", 0.1)

    async with sessions(async_engine)() as session:
        await UserRepository(session).get(users[0].id)
        await session.commit()
        links = RepositoryKeycloakLinks(session)
        async with links.organization_lock(alias):
            # Nothing uncommitted to lose: the failed rollback is logged, the connection invalidated.
            pass

    # After the rollback failure path, another connection can take the lock.
    assert await _try_organization_lock(async_engine, alias)


async def test_release_rollback_failure_preserves_body_exception(async_engine, members, monkeypatch):
    tenant, _role, _users = members
    alias = str(tenant.id)

    async def savepoint_fails(self, savepoint, alias: str) -> bool:
        return False

    async def failing_rollback(self: AsyncSession) -> None:
        raise SQLAlchemyError("rollback failed")

    monkeypatch.setattr(RepositoryKeycloakLinks, "_savepoint_rolled_back", savepoint_fails)
    monkeypatch.setattr(AsyncSession, "rollback", failing_rollback)
    monkeypatch.setattr(keycloak_sync, "ORGANIZATION_LOCK_RELEASE_SECONDS", 0.1)

    async with sessions(async_engine)() as session:
        await TenantRepository(session).get(tenant.id)
        await session.commit()
        links = RepositoryKeycloakLinks(session)
        with pytest.raises(BodyError):
            async with links.organization_lock(alias):
                # The body error is preserved even when rollback fails.
                raise BodyError("body failed under lock")

    assert await _try_organization_lock(async_engine, alias)


async def test_organization_lock_does_not_touch_rows_with_uncommitted_changes_before_lock(async_engine, members):
    tenant, _role, _users = members
    alias = str(tenant.id)

    async with sessions(async_engine)() as session:
        row = await TenantRepository(session).get(tenant.id)
        original_name = row.name
        row.name = "pre-lock-change"
        assert session.sync_session.is_modified(row)

        links = RepositoryKeycloakLinks(session)
        async with links.organization_lock(alias):
            # No further changes inside the lock: `_loaded` returns None because the session
            # already has uncommitted work. The nested savepoint rollback and expiration
            # of state must not commit the pre-lock change to the database.
            pass

        # Within the same session, the identity map still reflects the pre-lock change.
        row_after = await TenantRepository(session).get(tenant.id)
        assert row_after.name == "pre-lock-change"

    # A fresh session reads from the database and must still see the original value,
    # so the pre-lock change was not committed by the organization lock.
    async with sessions(async_engine)() as check:
        db_row = await TenantRepository(check).get(tenant.id)
        assert db_row.name == original_name


async def test_organization_lock_restores_rows_loaded_before_the_body(async_engine, members):
    tenant, _role, _users = members
    alias = str(tenant.id)

    async with sessions(async_engine)() as session:
        row = await TenantRepository(session).get(tenant.id)
        original_name = row.name
        links = RepositoryKeycloakLinks(session)

        async with links.organization_lock(alias):
            # A change inside the lock that is flushed is undone by the savepoint rollback,
            # and `_put_back` restores the pre-lock snapshot.
            row.name = "changed-under-lock"
            await session.flush()

        row_after = await TenantRepository(session).get(tenant.id)
        assert row_after.name == original_name
        assert not session.sync_session.is_modified(row_after)


async def test_an_invalidation_that_hangs_is_bounded_and_preserves_the_body_exception(
    async_engine, members, monkeypatch
):
    tenant, _role, _users = members
    alias = str(tenant.id)

    async def never_rollback(self, savepoint, alias: str) -> bool:
        await anyio.sleep_forever()
        return False

    monkeypatch.setattr(RepositoryKeycloakLinks, "_savepoint_rolled_back", never_rollback)
    monkeypatch.setattr(keycloak_sync, "ORGANIZATION_LOCK_RELEASE_SECONDS", 0.1)

    async with sessions(async_engine)() as session:
        links = RepositoryKeycloakLinks(session)
        original_invalidate = session.invalidate

        async def hanging_invalidate() -> None:
            await anyio.sleep_forever()

        session.invalidate = hanging_invalidate
        try:
            with anyio.fail_after(1) as outer:
                with pytest.raises(BodyError):
                    async with links.organization_lock(alias):
                        raise BodyError("body failed while discard_connection times out")
            # Cleanup is bounded: the outer timeout does not have to cancel the test.
            assert not outer.cancelled_caught
        finally:
            # Restore the session's invalidate method before the fixture tears it down.
            session.invalidate = original_invalidate
