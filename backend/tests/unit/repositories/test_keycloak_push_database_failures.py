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

from app.dependencies.keycloak_sync import RepositoryKeycloakLinks
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.adapters.base import PROVIDER_SYNC, PROVIDER_SYNC_FAILED
from app.services.adapters.keycloak import KeycloakSyncAdapter
from app.services.memberships import restore_memberships
from app.services.user import UserService

pytestmark = pytest.mark.asyncio


def sessions(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def failing_read(self, user_id: uuid.UUID, provider_name: str) -> str | None:
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
    monkeypatch.setattr(IdPLinkRepository, "signed_in_sub_of", failing_read)

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
    monkeypatch.setattr(IdPLinkRepository, "signed_in_sub_of", failing_read)
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
