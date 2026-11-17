"""The Zitadel canonical seed against the test database: what it writes, and that a rerun writes nothing."""

import dataclasses

import pytest
from sqlmodel import select

from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import IdPLink, User, UserStatus
from scripts.seed_zitadel import PROVIDER_NAME, LinkedUser, SeedConflictError, SeedInput, apply_seed

ISSUER = "http://zitadel.localhost:8480"

SEED = SeedInput(
    issuer_url=ISSUER,
    tenants={"acme": "399943487992119299", "globex": "399943487958564867"},
    linked_user=LinkedUser(
        external_sub="399943491398025219",
        email="acme-user@acme.test",
        user_name="acme-user",
        given_name="Acme",
        family_name="User",
        roles={"acme": "admin", "globex": "viewer"},
    ),
)


async def _state(session) -> dict:
    """Everything the seed writes, keyed by name, with no generated ids."""
    session.expire_all()
    tenants = (await session.execute(select(Tenant).where(Tenant.name.in_(["acme", "globex"])))).scalars().all()
    tenant_names = {tenant.id: tenant.name for tenant in tenants}
    provider = (await session.execute(select(Provider).where(Provider.name == PROVIDER_NAME))).scalars().one()
    user = (await session.execute(select(User).where(User.email == SEED.linked_user.email))).scalars().one()
    links = (await session.execute(select(IdPLink).where(IdPLink.user_id == user.id))).scalars().all()
    roles = (await session.execute(select(Role).where(Role.tenant_id.is_(None)))).scalars().all()
    role_names = {role.id: role.name for role in roles}
    assignments = (
        (await session.execute(select(UserTenantRole).where(UserTenantRole.user_id == user.id))).scalars().all()
    )
    return {
        "tenants": {
            tenant.name: (tenant.external_org_id, tenant.external_org_provider_id == provider.id) for tenant in tenants
        },
        "provider": (provider.type, provider.issuer_url, provider.base_url, provider.capabilities),
        "user": (user.user_name, user.given_name, user.family_name),
        "links": {(link.provider_id == provider.id, link.external_sub, link.external_email) for link in links},
        "assignments": {(tenant_names[a.tenant_id], role_names[a.role_id]) for a in assignments},
    }


EXPECTED = {
    "tenants": {"acme": ("399943487992119299", True), "globex": ("399943487958564867", True)},
    "provider": (ProviderType.oidc, ISSUER, ISSUER, ["sso"]),
    "user": ("acme-user", "Acme", "User"),
    "links": {(True, "399943491398025219", "acme-user@acme.test")},
    "assignments": {("acme", "admin"), ("globex", "viewer")},
}


async def test_the_seed_links_the_tenants_the_provider_and_the_acme_user(db_session):
    changes = await apply_seed(db_session, SEED)

    assert changes
    assert await _state(db_session) == EXPECTED


async def test_a_second_run_changes_nothing(db_session):
    await apply_seed(db_session, SEED)
    before = await _state(db_session)

    changes = await apply_seed(db_session, SEED)

    assert changes == []
    assert await _state(db_session) == before == EXPECTED


async def _refused(session, seed: SeedInput) -> SeedConflictError:
    """Run the seed, expect a refusal, and roll back as ``main`` does when it exits uncommitted."""
    with pytest.raises(SeedConflictError) as refusal:
        await apply_seed(session, seed)
    await session.rollback()
    return refusal.value


async def _provider_count(session) -> int:
    session.expire_all()
    return len((await session.execute(select(Provider).where(Provider.name == PROVIDER_NAME))).scalars().all())


async def test_a_new_instance_is_refused_and_leaves_the_store_as_it_was(db_session):
    await apply_seed(db_session, SEED)
    fresh = dataclasses.replace(
        SEED,
        tenants={"acme": "500000000000000001", "globex": "500000000000000002"},
        linked_user=dataclasses.replace(SEED.linked_user, external_sub="500000000000000003"),
    )

    await _refused(db_session, fresh)

    assert await _state(db_session) == EXPECTED


async def test_a_tenant_linked_to_another_organization_is_refused_not_moved(db_session):
    ory_org = "0b6f3c4e-7a52-4c1b-9d0e-2f9a8b7c6d5e"
    db_session.add(Tenant(name="acme", external_org_id=ory_org))
    await db_session.commit()

    refusal = await _refused(db_session, SEED)

    assert "acme" in str(refusal)
    acme = (await db_session.execute(select(Tenant).where(Tenant.name == "acme"))).scalars().one()
    assert acme.external_org_id == ory_org
    assert await _provider_count(db_session) == 0


async def test_a_tenant_with_no_organization_is_linked_to_its_organization(db_session):
    db_session.add(Tenant(name="acme"))
    await db_session.commit()

    await apply_seed(db_session, SEED)

    assert await _state(db_session) == EXPECTED


async def test_a_tenant_linked_to_its_organization_for_no_provider_is_linked_for_zitadel(db_session):
    """A tenant the seed linked before the link named its provider (migration 007) is the seed's own."""
    db_session.add(Tenant(name="acme", external_org_id=SEED.tenants["acme"]))
    await db_session.commit()

    await apply_seed(db_session, SEED)

    assert await _state(db_session) == EXPECTED


@pytest.mark.parametrize("external_org_id", [None, SEED.tenants["acme"]])
async def test_a_tenant_with_domains_is_refused_not_linked(db_session, external_org_id):
    """A tenant's domains admit its organization's first logins: domains written before the link are not the seed's."""
    db_session.add(Tenant(name="acme", domains=["acme.test"], external_org_id=external_org_id))
    await db_session.commit()

    refusal = await _refused(db_session, SEED)

    assert "acme" in str(refusal) and "domains" in str(refusal)
    db_session.expire_all()
    acme = (await db_session.execute(select(Tenant).where(Tenant.name == "acme"))).scalars().one()
    assert (acme.domains, acme.external_org_id, acme.external_org_provider_id) == (
        ["acme.test"],
        external_org_id,
        None,
    )
    assert await _provider_count(db_session) == 0


async def test_a_tenant_the_seed_linked_keeps_the_domains_written_since(db_session):
    """Domains an operator writes after the link are the operator's: a rerun neither refuses nor clears them."""
    await apply_seed(db_session, SEED)
    acme = (await db_session.execute(select(Tenant).where(Tenant.name == "acme"))).scalars().one()
    acme.domains = ["acme.test"]
    await db_session.commit()

    changes = await apply_seed(db_session, SEED)

    assert changes == []
    db_session.expire_all()
    acme = (await db_session.execute(select(Tenant).where(Tenant.name == "acme"))).scalars().one()
    assert acme.domains == ["acme.test"]


async def test_a_tenant_linked_to_its_organization_id_for_another_provider_is_refused(db_session):
    other = Provider(name="ory-local", type=ProviderType.ory)
    db_session.add(other)
    await db_session.flush()
    other_id = other.id
    db_session.add(Tenant(name="acme", external_org_id=SEED.tenants["acme"], external_org_provider_id=other_id))
    await db_session.commit()

    refusal = await _refused(db_session, SEED)

    assert "acme" in str(refusal)
    db_session.expire_all()
    acme = (await db_session.execute(select(Tenant).where(Tenant.name == "acme"))).scalars().one()
    assert (acme.external_org_id, acme.external_org_provider_id) == (SEED.tenants["acme"], other_id)
    assert await _provider_count(db_session) == 0


async def test_a_user_linked_by_another_provider_is_refused_not_linked_or_granted(db_session):
    other = Provider(name="ory-local", type=ProviderType.ory, issuer_url="http://ory.localhost")
    user = User(email=SEED.linked_user.email, user_name="someone-else")
    db_session.add_all([other, user])
    await db_session.flush()
    user_id, other_id = user.id, other.id
    db_session.add(IdPLink(user_id=user_id, provider_id=other_id, external_sub="ory-subject"))
    await db_session.commit()

    refusal = await _refused(db_session, SEED)

    assert SEED.linked_user.email in str(refusal)
    db_session.expire_all()
    links = (await db_session.execute(select(IdPLink).where(IdPLink.user_id == user_id))).scalars().all()
    assert {(link.provider_id, link.external_sub) for link in links} == {(other_id, "ory-subject")}
    grants = (await db_session.execute(select(UserTenantRole).where(UserTenantRole.user_id == user_id))).scalars()
    assert grants.all() == []


async def test_an_unlinked_user_with_the_same_email_is_refused_not_linked_or_granted(db_session):
    user = User(email=SEED.linked_user.email, user_name="someone-else")
    db_session.add(user)
    await db_session.flush()
    user_id = user.id
    await db_session.commit()

    await _refused(db_session, SEED)

    db_session.expire_all()
    links = (await db_session.execute(select(IdPLink).where(IdPLink.user_id == user_id))).scalars().all()
    assert links == []
    grants = (await db_session.execute(select(UserTenantRole).where(UserTenantRole.user_id == user_id))).scalars()
    assert grants.all() == []


async def _provider_row(session) -> tuple:
    session.expire_all()
    provider = (await session.execute(select(Provider).where(Provider.name == PROVIDER_NAME))).scalars().one()
    return (provider.type, provider.issuer_url, provider.base_url, provider.capabilities, provider.active)


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(
            {"issuer_url": "http://other.localhost:8480", "base_url": "http://other.localhost:8480"}, id="issuer"
        ),
        pytest.param({"type": ProviderType.ory}, id="type"),
        pytest.param({"active": False}, id="inactive"),
    ],
)
async def test_a_zitadel_provider_the_seed_did_not_write_is_refused_and_left_as_it_was(db_session, existing):
    row = {"type": ProviderType.oidc, "issuer_url": ISSUER, "base_url": ISSUER, "capabilities": ["sso"]} | existing
    db_session.add(Provider(name=PROVIDER_NAME, **row))
    await db_session.commit()
    before = await _provider_row(db_session)

    refusal = await _refused(db_session, SEED)

    assert PROVIDER_NAME in str(refusal)
    assert await _provider_row(db_session) == before
    tenants = (await db_session.execute(select(Tenant).where(Tenant.name.in_(["acme", "globex"])))).scalars()
    assert tenants.all() == []


async def test_an_organization_held_by_another_tenant_is_refused_not_shared(db_session):
    db_session.add(Tenant(name="acme-legacy", external_org_id=SEED.tenants["acme"]))
    await db_session.commit()

    refusal = await _refused(db_session, SEED)

    assert SEED.tenants["acme"] in str(refusal)
    db_session.expire_all()
    holders = (await db_session.execute(select(Tenant).where(Tenant.external_org_id == SEED.tenants["acme"]))).scalars()
    assert {tenant.name for tenant in holders} == {"acme-legacy"}
    assert await _provider_count(db_session) == 0


async def test_existing_canonical_roles_are_reused_not_duplicated(db_session):
    db_session.add(Role(name="admin", description="existing"))
    await db_session.flush()

    await apply_seed(db_session, SEED)

    admins = (await db_session.execute(select(Role).where(Role.name == "admin", Role.tenant_id.is_(None)))).scalars()
    assert [role.description for role in admins] == ["existing"]


@pytest.mark.parametrize(
    "external_org_id",
    [pytest.param(None, id="unlinked"), pytest.param(SEED.tenants["acme"], id="linked")],
)
async def test_a_suspended_tenant_is_refused_not_linked_or_granted(db_session, external_org_id):
    db_session.add(Tenant(name="acme", external_org_id=external_org_id, status=TenantStatus.suspended))
    await db_session.commit()

    refusal = await _refused(db_session, SEED)

    assert "acme" in str(refusal)
    db_session.expire_all()
    acme = (await db_session.execute(select(Tenant).where(Tenant.name == "acme"))).scalars().one()
    assert (acme.external_org_id, acme.status) == (external_org_id, TenantStatus.suspended)
    grants = (await db_session.execute(select(UserTenantRole).where(UserTenantRole.tenant_id == acme.id))).scalars()
    assert grants.all() == []
    assert await _provider_count(db_session) == 0


@pytest.mark.parametrize("status", [UserStatus.inactive, UserStatus.provisioned])
async def test_a_linked_user_that_is_not_active_is_refused_and_left_as_it_was(db_session, status):
    await apply_seed(db_session, SEED)
    user = (await db_session.execute(select(User).where(User.email == SEED.linked_user.email))).scalars().one()
    user.status = status
    await db_session.commit()
    before = await _state(db_session)

    refusal = await _refused(db_session, SEED)

    assert SEED.linked_user.email in str(refusal)
    assert await _state(db_session) == before
    db_session.expire_all()
    user = (await db_session.execute(select(User).where(User.email == SEED.linked_user.email))).scalars().one()
    assert user.status == status
