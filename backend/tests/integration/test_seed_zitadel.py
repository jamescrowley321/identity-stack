"""The Zitadel canonical seed against the test database: what it writes, and that a rerun writes nothing."""

import dataclasses

from sqlmodel import select

from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import IdPLink, User
from scripts.seed_zitadel import PROVIDER_NAME, LinkedUser, SeedInput, apply_seed

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
        "tenants": {tenant.name: tenant.external_org_id for tenant in tenants},
        "provider": (provider.type, provider.issuer_url, provider.base_url, provider.capabilities),
        "user": (user.user_name, user.given_name, user.family_name),
        "links": {(link.provider_id == provider.id, link.external_sub, link.external_email) for link in links},
        "assignments": {(tenant_names[a.tenant_id], role_names[a.role_id]) for a in assignments},
    }


EXPECTED = {
    "tenants": {"acme": "399943487992119299", "globex": "399943487958564867"},
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


async def test_a_new_instance_moves_the_tenants_and_the_link_to_its_ids(db_session):
    await apply_seed(db_session, SEED)
    fresh = dataclasses.replace(
        SEED,
        tenants={"acme": "500000000000000001", "globex": "500000000000000002"},
        linked_user=dataclasses.replace(SEED.linked_user, external_sub="500000000000000003"),
    )

    changes = await apply_seed(db_session, fresh)

    assert changes
    assert await _state(db_session) == {
        **EXPECTED,
        "tenants": {"acme": "500000000000000001", "globex": "500000000000000002"},
        "links": {(True, "500000000000000003", "acme-user@acme.test")},
    }


async def test_existing_canonical_roles_are_reused_not_duplicated(db_session):
    db_session.add(Role(name="admin", description="existing"))
    await db_session.flush()

    await apply_seed(db_session, SEED)

    admins = (await db_session.execute(select(Role).where(Role.name == "admin", Role.tenant_id.is_(None)))).scalars()
    assert [role.description for role in admins] == ["existing"]
