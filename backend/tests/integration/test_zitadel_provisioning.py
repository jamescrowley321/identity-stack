"""First-login provisioning into the tenant mapped from a Zitadel organization, against Postgres.

The tenants and the user come from the ``canonical_seed`` output recorded from a
real ``terraform apply`` (``tests/fixtures/zitadel/canonical-seed.json``): ``acme``
and ``globex`` are linked to their organizations through ``external_org_id``, for
the ``zitadel`` provider (``external_org_provider_id``). Each tenant lists the email
domains its organization's first logins may hold (``domains``).
"""

import json
import logging
import uuid
from pathlib import Path

import pytest
from sqlmodel import func, select

from app.errors.identity import EmailLinkRefused, Forbidden, NotFound, ValidationError
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
DOMAINS = {"acme": ["acme.test"], "globex": ["globex.test"]}
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


async def _seeded(
    session, *, globex_status: TenantStatus = TenantStatus.active, domains: dict[str, list[str]] = DOMAINS
) -> uuid.UUID:
    """The provider row and the two linked tenants, as ``scripts/seed_zitadel.py`` writes them, with ``domains``.

    The provider's id.
    """
    provider = Provider(name=PROVIDER, type=ProviderType.oidc, issuer_url=ISSUER, base_url=ISSUER, capabilities=["sso"])
    session.add(provider)
    await session.flush()
    provider_id = provider.id
    session.add(
        Tenant(name="acme", domains=domains["acme"], external_org_id=ACME_ORG, external_org_provider_id=provider_id)
    )
    session.add(
        Tenant(
            name="globex",
            domains=domains["globex"],
            external_org_id=GLOBEX_ORG,
            external_org_provider_id=provider_id,
            status=globex_status,
        )
    )
    await session.flush()
    return provider_id


async def _other_provider(session, name: str = "ory") -> uuid.UUID:
    provider = Provider(name=name, type=ProviderType.ory)
    session.add(provider)
    await session.flush()
    return provider.id


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

    result = await _provision(db_session, ACME_ORG, email="acme-member@acme.test")

    assert result.is_ok(), result
    assert await _count(db_session, Role) == before
    assert await _memberships(db_session, result.ok) == {("acme", MEMBER, True)}


async def test_another_providers_tenant_with_the_same_organization_id_is_not_counted(db_session):
    await _seeded(db_session)
    ory_id = await _other_provider(db_session)
    db_session.add(Tenant(name="ory-tenant", external_org_id=GLOBEX_ORG, external_org_provider_id=ory_id))
    await db_session.flush()

    result = await _provision(db_session, GLOBEX_ORG)

    assert result.is_ok(), result
    assert await _memberships(db_session, result.ok) == {("globex", MEMBER, True)}


class TestTheOrganizationsTenant:
    """``organization_tenant_id``, asked before UserInfo is fetched, scopes the link to the provider."""

    async def test_is_the_tenant_linked_for_that_provider(self, db_session):
        await _seeded(db_session)
        ory_id = await _other_provider(db_session)
        db_session.add(Tenant(name="ory-tenant", external_org_id=GLOBEX_ORG, external_org_provider_id=ory_id))
        await db_session.flush()
        globex = await TenantRepository(db_session).get_by_name("globex")

        result = await _service(db_session).organization_tenant_id(PROVIDER, GLOBEX_ORG)

        assert result.is_ok() and result.ok == globex.id, result

    async def test_without_the_provider_row_is_not_found(self, db_session):
        db_session.add(Tenant(name="globex", external_org_id=GLOBEX_ORG))
        await db_session.flush()

        result = await _service(db_session).organization_tenant_id(PROVIDER, GLOBEX_ORG)

        assert type(result.error) is NotFound


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
        provider_id = await _seeded(db_session)
        db_session.add(Tenant(name="globex-copy", external_org_id=GLOBEX_ORG, external_org_provider_id=provider_id))
        await db_session.flush()

        await self._refused(db_session, GLOBEX_ORG, Forbidden)

    async def test_an_organization_id_linked_only_for_another_provider(self, db_session):
        """``external_org_id`` is shared by the providers: the same string from another one is not Zitadel's."""
        await _seeded(db_session)
        ory_id = await _other_provider(db_session)
        db_session.add(Tenant(name="ory-tenant", external_org_id=UNMAPPED_ORG, external_org_provider_id=ory_id))
        await db_session.flush()

        await self._refused(db_session, UNMAPPED_ORG, Forbidden)

    async def test_an_organization_id_linked_for_no_provider(self, db_session):
        await _seeded(db_session)
        db_session.add(Tenant(name="unowned", external_org_id=UNMAPPED_ORG))
        await db_session.flush()

        await self._refused(db_session, UNMAPPED_ORG, Forbidden)

    async def test_without_the_provider_row(self, db_session):
        db_session.add(Tenant(name="globex", external_org_id=GLOBEX_ORG))
        await db_session.flush()

        await self._refused(db_session, GLOBEX_ORG, NotFound)

    async def test_the_organization_of_a_suspended_tenant(self, db_session):
        await _seeded(db_session, globex_status=TenantStatus.suspended)

        await self._refused(db_session, GLOBEX_ORG, Forbidden)

    async def test_a_missing_email(self, db_session):
        await _seeded(db_session)

        await self._refused(db_session, GLOBEX_ORG, ValidationError, email="")

    async def test_an_unverified_email(self, db_session):
        await _seeded(db_session)

        await self._refused(db_session, GLOBEX_ORG, ValidationError, email_verified=False)


class TestAnEmailAlreadyHeldIsNeverLinked:
    """A tenant organization's administrators choose its users' emails and can mark them verified.

    So an organization's first login never takes over the canonical user who
    already holds that email, whatever tenant or provider that user came from:
    linking it would hand them that user's roles in every tenant. The refusal is
    ``Forbidden``, the answer an unlinked organization gets, so the login does
    not learn that the email is held.
    """

    async def _acme_admin(self, session, email: str = EMAIL) -> uuid.UUID:
        """An acme admin holding ``email``; its id, read before anything expires the row."""
        user = User(email=email, user_name=email)
        role = Role(name="admin")
        session.add_all([user, role])
        await session.flush()
        acme = await TenantRepository(session).get_by_name("acme")
        session.add(UserTenantRole(user_id=user.id, tenant_id=acme.id, role_id=role.id))
        await session.flush()
        return user.id

    async def _refused(self, session, organization_id) -> None:
        before = await _written(session)

        result = await _provision(session, organization_id)

        assert result.is_error()
        assert type(result.error) is Forbidden
        assert await _written(session) == before
        assert await IdPLinkRepository(session).get_by_provider_name_and_sub(PROVIDER, SUB) is None

    async def test_a_globex_login_with_an_acme_users_email_links_and_grants_nothing(self, db_session):
        await _seeded(db_session)
        admin_id = await self._acme_admin(db_session)

        await self._refused(db_session, GLOBEX_ORG)

        assert await _memberships(db_session, admin_id) == {("acme", "admin", True)}

    async def test_the_refusal_logs_the_holders_id_not_the_email(self, db_session, caplog):
        await _seeded(db_session)
        admin_id = await self._acme_admin(db_session)

        with caplog.at_level(logging.WARNING, logger="app.services.identity_provisioning"):
            await self._refused(db_session, GLOBEX_ORG)

        refusals = [r.getMessage() for r in caplog.records if r.name == "app.services.identity_provisioning"]
        assert len(refusals) == 1, refusals
        assert str(admin_id) in refusals[0] and PROVIDER in refusals[0] and SUB in refusals[0]
        assert EMAIL not in refusals[0]

    async def test_a_login_in_the_users_own_organization_is_refused_too(self, db_session):
        await _seeded(db_session)
        await self._acme_admin(db_session, email="admin@acme.test")

        before = await _written(db_session)
        result = await _provision(db_session, ACME_ORG, email="admin@acme.test")

        assert result.is_error()
        assert type(result.error) is Forbidden
        assert await _written(db_session) == before
        assert await IdPLinkRepository(db_session).get_by_provider_name_and_sub(PROVIDER, SUB) is None

    async def test_a_user_with_no_membership_is_not_linked_either(self, db_session):
        await _seeded(db_session)
        db_session.add(User(email=EMAIL, user_name=EMAIL))
        await db_session.flush()

        await self._refused(db_session, GLOBEX_ORG)


class TestAnOrganizationLoginOutsideItsTenantsDomains:
    """An organization's administrators can create a user with any email, someone else's included.

    So an organization's first login creates a user only for an email in one of
    its tenant's ``domains``: otherwise an administrator of globex could take
    ``ceo@acme.test`` and refuse the real holder's first login through every
    other provider. Outside them the login gets ``Forbidden``, as an unlinked
    organization does, and nothing is written, so it learns nothing about the
    email either.
    """

    async def _refused(self, session, **changes) -> None:
        before = await _written(session)

        result = await _provision(session, GLOBEX_ORG, **changes)

        assert result.is_error()
        assert type(result.error) is Forbidden
        assert await _written(session) == before
        assert await IdPLinkRepository(session).get_by_provider_name_and_sub(PROVIDER, SUB) is None

    async def test_another_tenants_domain_is_refused(self, db_session):
        await _seeded(db_session)

        await self._refused(db_session, email="ceo@acme.test")

    async def test_an_email_no_canonical_user_holds_is_refused_too(self, db_session):
        """Refused before any lookup by email: held or not, the answer is the same."""
        await _seeded(db_session)

        await self._refused(db_session, email="nobody@elsewhere.test")

        assert await UserRepository(db_session).get_by_email("nobody@elsewhere.test") is None

    async def test_an_email_held_outside_the_domains_gets_the_same_answer(self, db_session):
        await _seeded(db_session)
        db_session.add(User(email="ceo@acme.test", user_name="ceo@acme.test"))
        await db_session.flush()

        await self._refused(db_session, email="ceo@acme.test")

    @pytest.mark.parametrize(
        "email",
        [
            "user@sub.globex.test",
            "user@evilglobex.test",
            "user@globex.test.evil.test",
            "globex.test",
            "@globex.test",
            "user@acme.test@globex.test",
            "user@globex.test ",
        ],
    )
    async def test_a_domain_is_matched_whole(self, db_session, email):
        await _seeded(db_session)

        await self._refused(db_session, email=email)

    async def test_a_tenant_with_no_domains_admits_no_first_login(self, db_session):
        await _seeded(db_session, domains={**DOMAINS, "globex": []})

        await self._refused(db_session)

    async def test_the_domain_is_compared_without_case(self, db_session):
        await _seeded(db_session, domains={**DOMAINS, "globex": ["Globex.TEST"]})

        result = await _provision(db_session, GLOBEX_ORG, email="Globex-User@GLOBEX.test")

        assert result.is_ok(), result
        assert await _memberships(db_session, result.ok) == {("globex", MEMBER, True)}

    @pytest.mark.parametrize(
        "domain", [" globex.test", "globex.test.", "@globex.test", "Globex.TEST", " @GLOBEX.test. "]
    )
    async def test_a_tenant_domain_is_read_without_spaces_at_sign_or_trailing_dot(self, db_session, domain):
        await _seeded(db_session, domains={**DOMAINS, "globex": [domain]})

        result = await _provision(db_session, GLOBEX_ORG)

        assert result.is_ok(), result
        assert await _memberships(db_session, result.ok) == {("globex", MEMBER, True)}

    async def test_the_emails_domain_is_read_without_a_trailing_dot(self, db_session):
        await _seeded(db_session)

        result = await _provision(db_session, GLOBEX_ORG, email="globex-user@globex.test.")

        assert result.is_ok(), result
        assert await _memberships(db_session, result.ok) == {("globex", MEMBER, True)}

    async def test_the_refusal_logs_the_tenant_not_the_email(self, db_session, caplog):
        await _seeded(db_session)
        globex = await TenantRepository(db_session).get_by_name("globex")
        globex_id = globex.id

        with caplog.at_level(logging.WARNING, logger="app.services.identity_provisioning"):
            await self._refused(db_session, email="ceo@acme.test")

        refusals = [r.getMessage() for r in caplog.records if r.name == "app.services.identity_provisioning"]
        assert len(refusals) == 1, refusals
        assert str(globex_id) in refusals[0] and PROVIDER in refusals[0] and SUB in refusals[0]
        assert "ceo@acme.test" not in refusals[0]

    async def test_a_user_linked_beforehand_signs_in_whatever_the_domains(self, db_session):
        """The link is found before any email is read: an administrator's ``idp_links`` row still admits its user."""
        provider_id = await _seeded(db_session, domains={"acme": [], "globex": []})
        user = User(email="ceo@acme.test", user_name="ceo@acme.test")
        db_session.add(user)
        await db_session.flush()
        user_id = user.id
        db_session.add(IdPLink(user_id=user_id, provider_id=provider_id, external_sub=SUB))
        await db_session.flush()

        result = await _provision(db_session, GLOBEX_ORG, email="ceo@acme.test")

        assert result.is_ok() and result.ok == user_id, result


class TestAnOrganizationsEmailIsNeverLinkedFromAnotherProvider:
    """The other direction: a user an organization's first login created holds an email that
    organization's administrators chose, so no other provider's first login links to it by
    email either, however verified that provider says the email is.
    """

    async def _zitadel_user(self, session) -> uuid.UUID:
        await _seeded(session)
        await _other_provider(session)
        created = await _provision(session, GLOBEX_ORG)
        assert created.is_ok(), created
        return created.ok

    async def test_the_created_user_is_marked_as_holding_an_organizations_email(self, db_session):
        user_id = await self._zitadel_user(db_session)

        user = await UserRepository(db_session).get_by_email(EMAIL)

        assert (user.id, user.email_from_organization) == (user_id, True)

    async def test_an_ory_first_login_with_that_email_links_and_grants_nothing(self, db_session):
        user_id = await self._zitadel_user(db_session)
        before = await _written(db_session)

        result = await _service(db_session).provision(
            provider_name="ory", sub="ory-victim", email=EMAIL, email_verified=True
        )

        assert result.is_error()
        assert type(result.error) is EmailLinkRefused
        assert await _written(db_session) == before
        assert await IdPLinkRepository(db_session).get_by_provider_name_and_sub("ory", "ory-victim") is None
        assert await _memberships(db_session, user_id) == {("globex", MEMBER, True)}

    async def test_that_refusal_logs_the_holders_id_not_the_email(self, db_session, caplog):
        user_id = await self._zitadel_user(db_session)

        with caplog.at_level(logging.WARNING, logger="app.services.identity_provisioning"):
            await _service(db_session).provision(
                provider_name="ory", sub="ory-victim", email=EMAIL, email_verified=True
            )

        refusals = [r.getMessage() for r in caplog.records if r.name == "app.services.identity_provisioning"]
        assert len(refusals) == 1, refusals
        assert str(user_id) in refusals[0] and "ory" in refusals[0] and "ory-victim" in refusals[0]
        assert EMAIL not in refusals[0]

    async def test_an_ory_first_login_still_links_a_user_whose_email_no_organization_chose(self, db_session):
        await self._zitadel_user(db_session)
        other = User(email="ory-user@example.test", user_name="ory-user@example.test")
        db_session.add(other)
        await db_session.flush()
        other_id = other.id

        result = await _service(db_session).provision(
            provider_name="ory", sub="ory-user", email="ory-user@example.test", email_verified=True
        )

        assert result.is_ok() and result.ok == other_id, result


class _StaleFirstRead(IdPLinkRepository):
    """Answers the first (provider, sub) lookup as a login racing another one would: no link yet."""

    def __init__(self, session) -> None:
        super().__init__(session)
        self._reads = 0

    async def get_by_provider_and_sub(self, provider_id, external_sub):
        self._reads += 1
        if self._reads == 1:
            return None
        return await super().get_by_provider_and_sub(provider_id, external_sub)


async def test_a_login_that_lost_the_race_for_its_own_subject_gets_the_winners_user(db_session):
    await _seeded(db_session)
    winner = await _provision(db_session, GLOBEX_ORG)
    assert winner.is_ok(), winner
    before = await _written(db_session)
    service = IdentityProvisioningService(
        session=db_session,
        provider_repository=ProviderRepository(db_session),
        idp_link_repository=_StaleFirstRead(db_session),
        user_repository=UserRepository(db_session),
        tenant_repository=TenantRepository(db_session),
        role_repository=RoleRepository(db_session),
        assignment_repository=UserTenantRoleRepository(db_session),
    )

    result = await service.provision(
        provider_name=PROVIDER, sub=SUB, email=EMAIL, email_verified=True, organization_id=GLOBEX_ORG
    )

    assert result.is_ok() and result.ok == winner.ok, result
    assert await _written(db_session) == before
