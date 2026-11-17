"""First-login provisioning into the tenant mapped from a Zitadel organization, against Postgres.

The tenants and the user come from the ``canonical_seed`` output recorded from a
real ``terraform apply`` (``tests/fixtures/zitadel/canonical-seed.json``): ``acme``
and ``globex`` are linked to their organizations through ``external_org_id``.
"""

import json
from pathlib import Path

from sqlmodel import func, select

from app.errors.identity import Forbidden, ValidationError
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import IdPLink, User
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.identity_provisioning import DEFAULT_TENANT_NAME, IdentityProvisioningService

SEED = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "zitadel" / "canonical-seed.json").read_text())
ISSUER = SEED["issuer_url"]
ACME_ORG = SEED["tenants"]["acme"]
GLOBEX_ORG = SEED["tenants"]["globex"]
UNMAPPED_ORG = "399948686400000000"
SUB = "399948690000000001"
EMAIL = "globex-user@globex.test"
PROVIDER = "zitadel"
MEMBER = "member"


def _service(session) -> IdentityProvisioningService:
    return IdentityProvisioningService(
        session=session,
        provider_repository=ProviderRepository(session),
        idp_link_repository=IdPLinkRepository(session),
        user_repository=UserRepository(session),
        tenant_repository=TenantRepository(session),
        role_repository=RoleRepository(session),
        assignment_repository=UserTenantRoleRepository(session),
    )


async def _seeded(session, *, globex_status: TenantStatus = TenantStatus.active) -> None:
    """The provider row and the two linked tenants, as ``scripts/seed_zitadel.py`` writes them."""
    session.add(
        Provider(name=PROVIDER, type=ProviderType.oidc, issuer_url=ISSUER, base_url=ISSUER, capabilities=["sso"])
    )
    session.add(Tenant(name="acme", external_org_id=ACME_ORG))
    session.add(Tenant(name="globex", external_org_id=GLOBEX_ORG, status=globex_status))
    await session.flush()


async def _provision(session, organization_id: str | None, **changes):
    arguments = {
        "provider_name": PROVIDER,
        "sub": SUB,
        "email": EMAIL,
        "email_verified": True,
        "given_name": "Globex",
        "family_name": "User",
        "organization_id": organization_id,
        **changes,
    }
    return await _service(session).provision(**arguments)


async def _count(session, model) -> int:
    session.expire_all()
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _written(session) -> dict:
    """What provisioning may write, counted."""
    return {
        "users": await _count(session, User),
        "links": await _count(session, IdPLink),
        "assignments": await _count(session, UserTenantRole),
        "roles": await _count(session, Role),
        "tenants": await _count(session, Tenant),
    }


async def _memberships(session, user_id) -> set[tuple[str, str, bool]]:
    """(tenant name, role name, role is global) for each of the user's assignments."""
    rows = await session.execute(
        select(Tenant.name, Role.name, Role.tenant_id.is_(None))
        .join(UserTenantRole, UserTenantRole.tenant_id == Tenant.id)
        .join(Role, Role.id == UserTenantRole.role_id)
        .where(UserTenantRole.user_id == user_id)
    )
    return {tuple(row) for row in rows.all()}


async def test_a_first_login_joins_the_organizations_tenant_as_member(db_session):
    await _seeded(db_session)

    result = await _provision(db_session, GLOBEX_ORG)

    assert result.is_ok(), result
    link = await IdPLinkRepository(db_session).get_by_provider_name_and_sub(PROVIDER, SUB)
    assert link is not None
    assert (link.user_id, link.external_email) == (result.ok, EMAIL)
    user = await UserRepository(db_session).get_by_email(EMAIL)
    assert user is not None
    assert (user.id, user.given_name, user.family_name) == (result.ok, "Globex", "User")
    assert await _memberships(db_session, result.ok) == {("globex", MEMBER, True)}
    assert await TenantRepository(db_session).get_by_name(DEFAULT_TENANT_NAME) is None


async def test_an_existing_global_member_role_is_reused(db_session):
    await _seeded(db_session)
    db_session.add(Role(name=MEMBER))
    await db_session.flush()
    before = await _count(db_session, Role)

    result = await _provision(db_session, ACME_ORG)

    assert result.is_ok(), result
    assert await _count(db_session, Role) == before
    assert await _memberships(db_session, result.ok) == {("acme", MEMBER, True)}


async def test_a_second_login_changes_nothing(db_session):
    await _seeded(db_session)
    first = await _provision(db_session, GLOBEX_ORG)
    before = await _written(db_session)

    second = await _provision(db_session, GLOBEX_ORG)

    assert second.is_ok() and second.ok == first.ok
    assert await _written(db_session) == before


class TestARefusedFirstLoginWritesNothing:
    async def _refused(self, session, organization_id, error_type, **changes):
        before = await _written(session)

        result = await _provision(session, organization_id, **changes)

        assert result.is_error()
        assert type(result.error) is error_type
        assert await _written(session) == before

    async def test_an_organization_no_tenant_names(self, db_session):
        await _seeded(db_session)

        await self._refused(db_session, UNMAPPED_ORG, Forbidden)

    async def test_an_organization_two_tenants_name(self, db_session):
        await _seeded(db_session)
        db_session.add(Tenant(name="globex-copy", external_org_id=GLOBEX_ORG))
        await db_session.flush()

        await self._refused(db_session, GLOBEX_ORG, Forbidden)

    async def test_the_organization_of_a_suspended_tenant(self, db_session):
        await _seeded(db_session, globex_status=TenantStatus.suspended)

        await self._refused(db_session, GLOBEX_ORG, Forbidden)

    async def test_a_missing_email(self, db_session):
        await _seeded(db_session)

        await self._refused(db_session, GLOBEX_ORG, ValidationError, email="")

    async def test_an_unverified_email(self, db_session):
        await _seeded(db_session)

        await self._refused(db_session, GLOBEX_ORG, ValidationError, email_verified=False)
