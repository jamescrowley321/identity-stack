"""A Keycloak push whose own database work fails, on a real request session (Postgres).

The push runs after the service committed, then the service reads the rows it
loaded before the push. A failed IdP link or tenant link read must leave
those rows loaded and the session usable: the committed change answers with
``provider_sync: failed`` (or logs), it does not raise.
"""

import logging
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dependencies.keycloak_sync import RepositoryKeycloakLinks, UncommittedWorkLost
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
