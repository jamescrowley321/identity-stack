"""RepositoryZitadelLinks against Postgres: what ZitadelSyncAdapter reads and writes canonically.

A tenant's organization is linked for the ``zitadel`` provider row
(``external_org_id`` with ``external_org_provider_id``), and a created user's Zitadel id
is its ``zitadel`` IdP link: the rows first-login provisioning reads, so a Zitadel login
into that organization, or by that user, finds them.
"""

import uuid

import pytest

from app.dependencies.zitadel_sync import RepositoryZitadelLinks, TenantLinkedElsewhere
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import LINK_ORIGIN, LINKED_AT_SIGN_IN, LINKED_BY_BACKEND, IdPLink, User
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.base import RepositoryConflictError
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.identity_provisioning import IdentityProvisioningService

ISSUER = "http://zitadel.localhost:8480"
ORG_ID = "399979343167631363"
OTHER_ORG_ID = "399979343167631999"


async def _provider(session, name: str = "zitadel") -> uuid.UUID:
    provider = Provider(name=name, type=ProviderType.oidc, issuer_url=ISSUER, base_url=ISSUER, capabilities=["sso"])
    session.add(provider)
    await session.flush()
    return provider.id


async def _tenant(session, name: str = "initech", **links) -> uuid.UUID:
    tenant = Tenant(name=name, **links)
    session.add(tenant)
    await session.flush()
    return tenant.id


async def _user(session, email: str = "ada@initech.test") -> uuid.UUID:
    user = User(email=email, user_name="ada", given_name="Ada", family_name="Lovelace")
    session.add(user)
    await session.flush()
    return user.id


async def test_the_provider_is_the_zitadel_row(db_session):
    links = RepositoryZitadelLinks(db_session)
    assert await links.provider_id() is None

    provider_id = await _provider(db_session)

    assert await links.provider_id() == provider_id


async def test_linking_a_tenant_records_the_organization_for_the_zitadel_provider(db_session):
    provider_id = await _provider(db_session)
    tenant_id = await _tenant(db_session)
    links = RepositoryZitadelLinks(db_session)

    await links.link_organization(tenant_id, provider_id, ORG_ID)

    assert await links.organization_of(tenant_id, provider_id) == ORG_ID
    found = await TenantRepository(db_session).list_by_external_org_id(provider_id, ORG_ID)
    assert [tenant.id for tenant in found] == [tenant_id]


async def test_a_tenant_linked_for_another_provider_has_no_zitadel_organization(db_session):
    provider_id = await _provider(db_session)
    other_id = await _provider(db_session, "keycloak")
    tenant_id = await _tenant(db_session, external_org_id=ORG_ID, external_org_provider_id=other_id)

    assert await RepositoryZitadelLinks(db_session).organization_of(tenant_id, provider_id) is None


@pytest.mark.parametrize(
    "existing",
    [{"provider": "keycloak", "org": ORG_ID}, {"provider": "zitadel", "org": OTHER_ORG_ID}],
    ids=["another provider", "another organization"],
)
async def test_a_tenant_linked_to_anything_else_is_not_relinked(db_session, existing):
    provider_id = await _provider(db_session)
    owner_id = provider_id if existing["provider"] == "zitadel" else await _provider(db_session, existing["provider"])
    tenant_id = await _tenant(db_session, external_org_id=existing["org"], external_org_provider_id=owner_id)

    with pytest.raises(TenantLinkedElsewhere):
        await RepositoryZitadelLinks(db_session).link_organization(tenant_id, provider_id, ORG_ID)

    db_session.expire_all()
    tenant = await TenantRepository(db_session).get(tenant_id)
    assert (tenant.external_org_id, tenant.external_org_provider_id) == (existing["org"], owner_id)


async def test_linking_a_tenant_again_to_the_same_organization_is_allowed(db_session):
    provider_id = await _provider(db_session)
    tenant_id = await _tenant(db_session, external_org_id=ORG_ID, external_org_provider_id=provider_id)

    await RepositoryZitadelLinks(db_session).link_organization(tenant_id, provider_id, ORG_ID)

    assert await RepositoryZitadelLinks(db_session).organization_of(tenant_id, provider_id) == ORG_ID


@pytest.mark.parametrize(
    ("linked", "expected"),
    [("none", False), ("zitadel", False), ("keycloak", True), ("no provider", True)],
)
async def test_a_tenant_is_linked_elsewhere_when_its_organization_is_not_one_of_zitadel(db_session, linked, expected):
    provider_id = await _provider(db_session)
    owners = {"none": None, "zitadel": provider_id, "no provider": None}
    owner_id = owners[linked] if linked in owners else await _provider(db_session, linked)
    org_id = None if linked == "none" else ORG_ID
    tenant_id = await _tenant(db_session, external_org_id=org_id, external_org_provider_id=owner_id)

    assert await RepositoryZitadelLinks(db_session).linked_elsewhere(tenant_id, provider_id) is expected


async def test_a_failed_organization_link_leaves_the_session_usable(db_session):
    """The session is the request's: a failed write is rolled back, not left for the next use to trip on."""
    provider_id = await _provider(db_session)
    tenant_id = await _tenant(db_session)
    await db_session.commit()
    links = RepositoryZitadelLinks(db_session)

    with pytest.raises(RepositoryConflictError):
        # No such provider row: the foreign key refuses the write.
        await links.link_organization(tenant_id, uuid.uuid4(), ORG_ID)

    assert await links.provider_id() == provider_id
    assert await links.organization_of(tenant_id, provider_id) is None


async def test_linking_the_same_subject_again_is_done_and_leaves_the_session_usable(db_session):
    """A concurrent sync wrote the same link first: the unique key refuses the second, which counts as done."""
    provider_id = await _provider(db_session)
    user_id = await _user(db_session)
    await db_session.commit()
    links = RepositoryZitadelLinks(db_session)
    await links.link_subject(user_id, provider_id, str(user_id), "ada@initech.test")

    await links.link_subject(user_id, provider_id, str(user_id), "ada@initech.test")

    assert await links.subject_of(user_id, provider_id) == str(user_id)


async def test_a_subject_another_user_holds_is_refused_and_leaves_the_session_usable(db_session):
    provider_id = await _provider(db_session)
    holder_id = await _user(db_session, "holder@initech.test")
    user_id = await _user(db_session)
    await db_session.commit()
    links = RepositoryZitadelLinks(db_session)
    await links.link_subject(holder_id, provider_id, str(user_id), "holder@initech.test")

    with pytest.raises(RepositoryConflictError):
        await links.link_subject(user_id, provider_id, str(user_id), "ada@initech.test")

    assert await links.subject_of(user_id, provider_id) is None
    assert await links.subject_of(holder_id, provider_id) == str(user_id)


async def test_linking_a_tenant_that_does_not_exist_raises(db_session):
    provider_id = await _provider(db_session)

    with pytest.raises(LookupError):
        await RepositoryZitadelLinks(db_session).link_organization(uuid.uuid4(), provider_id, ORG_ID)


async def test_a_linked_subject_is_the_users_zitadel_idp_link(db_session):
    provider_id = await _provider(db_session)
    user_id = await _user(db_session)
    links = RepositoryZitadelLinks(db_session)
    assert await links.subject_of(user_id, provider_id) is None

    await links.link_subject(user_id, provider_id, str(user_id), "ada@initech.test")

    assert await links.subject_of(user_id, provider_id) == str(user_id)
    link = await IdPLinkRepository(db_session).get_by_provider_and_sub(provider_id, str(user_id))
    assert (link.user_id, link.external_email, link.metadata_) == (
        user_id,
        "ada@initech.test",
        {LINK_ORIGIN: LINKED_BY_BACKEND},
    )


async def test_a_link_a_sign_in_made_is_followed(db_session):
    provider_id = await _provider(db_session)
    user_id = await _user(db_session)
    db_session.add(
        IdPLink(
            user_id=user_id,
            provider_id=provider_id,
            external_sub="399979638798954499",
            metadata_={LINK_ORIGIN: LINKED_AT_SIGN_IN},
        )
    )
    await db_session.flush()

    assert await RepositoryZitadelLinks(db_session).subject_of(user_id, provider_id) == "399979638798954499"


@pytest.mark.parametrize("metadata", [None, {}, {"note": "typed in"}], ids=["none", "empty", "other-keys"])
async def test_a_link_an_admin_made_is_not_followed(db_session, metadata):
    """An IdP link a tenant admin made (with no LINK_ORIGIN) is not followed.

    Its subject is whatever the admin typed in.
    """
    provider_id = await _provider(db_session)
    user_id = await _user(db_session)
    db_session.add(
        IdPLink(user_id=user_id, provider_id=provider_id, external_sub="399979638798950001", metadata_=metadata)
    )
    await db_session.flush()

    assert await RepositoryZitadelLinks(db_session).subject_of(user_id, provider_id) is None


async def test_a_link_of_another_provider_is_not_the_zitadel_subject(db_session):
    provider_id = await _provider(db_session)
    other_id = await _provider(db_session, "ory")
    user_id = await _user(db_session)
    await RepositoryZitadelLinks(db_session).link_subject(user_id, other_id, "ory-sub", "ada@initech.test")

    assert await RepositoryZitadelLinks(db_session).subject_of(user_id, provider_id) is None


async def test_the_first_zitadel_login_of_a_created_user_is_that_user(db_session):
    """Its email belongs to a canonical user, so without the link the login would be refused (EmailLinkRefused)."""
    provider_id = await _provider(db_session)
    await _tenant(db_session, external_org_id=ORG_ID, external_org_provider_id=provider_id)
    user_id = await _user(db_session)
    await RepositoryZitadelLinks(db_session).link_subject(user_id, provider_id, str(user_id), "ada@initech.test")
    provisioning = IdentityProvisioningService(
        session=db_session,
        provider_repository=ProviderRepository(db_session),
        idp_link_repository=IdPLinkRepository(db_session),
        user_repository=UserRepository(db_session),
        tenant_repository=TenantRepository(db_session),
        role_repository=RoleRepository(db_session),
        assignment_repository=UserTenantRoleRepository(db_session),
    )

    result = await provisioning.provision(
        provider_name="zitadel",
        sub=str(user_id),
        email="ada@initech.test",
        email_verified=True,
        given_name="Ada",
        family_name="Lovelace",
        organization_id=ORG_ID,
    )

    assert result.is_ok(), result
    assert result.ok == user_id


@pytest.mark.parametrize(
    "metadata",
    [None, {"note": "typed in"}, {LINK_ORIGIN: LINKED_AT_SIGN_IN}, {LINK_ORIGIN: LINKED_BY_BACKEND}],
    ids=["none", "other-keys", "sign-in", "backend"],
)
async def test_a_zitadel_link_of_any_origin_is_a_link(db_session, metadata):
    provider_id = await _provider(db_session)
    other_id = await _provider(db_session, "ory")
    user_id = await _user(db_session)
    links = RepositoryZitadelLinks(db_session)
    db_session.add(IdPLink(user_id=user_id, provider_id=other_id, external_sub="ory-sub", metadata_=metadata))
    await db_session.flush()
    assert await links.has_link(user_id, provider_id) is False

    db_session.add(
        IdPLink(user_id=user_id, provider_id=provider_id, external_sub="399979638798950001", metadata_=metadata)
    )
    await db_session.flush()

    assert await links.has_link(user_id, provider_id) is True


async def test_the_users_organizations_are_those_of_its_tenants_linked_to_zitadel(db_session):
    provider_id = await _provider(db_session)
    other_id = await _provider(db_session, "keycloak")
    member_of = await _tenant(db_session, external_org_id=ORG_ID, external_org_provider_id=provider_id)
    elsewhere = await _tenant(db_session, "globex", external_org_id=OTHER_ORG_ID, external_org_provider_id=other_id)
    unlinked = await _tenant(db_session, "umbrella")
    await _tenant(db_session, "hooli", external_org_id="399979343167632000", external_org_provider_id=provider_id)
    user_id = await _user(db_session)
    other_user = await _user(db_session, "bob@initech.test")
    role = await RoleRepository(db_session).create(Role(name="member"))
    assignments = UserTenantRoleRepository(db_session)
    for tenant_id in (member_of, elsewhere, unlinked):
        await assignments.create(UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=role.id))
    await db_session.flush()
    links = RepositoryZitadelLinks(db_session)

    assert await links.organizations_of_user(user_id, provider_id) == frozenset({ORG_ID})
    assert await links.organizations_of_user(other_user, provider_id) == frozenset()
