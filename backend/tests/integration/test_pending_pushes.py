"""The pending-push triggers (migration 009) against the test Postgres: what marks a canonical change undelivered.

Each test empties ``pending_pushes`` in its own transaction (rolled back after it)
and writes canonical rows as the services do.
"""

import uuid

import pytest_asyncio
from sqlalchemy import delete, select

from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Permission, Role, RolePermission
from app.models.identity.sync_ledger import PendingPush
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import User, UserStatus
from app.repositories.sync_ledger import PendingPushRepository
from scripts.seed_keycloak import link_organizations_in


@pytest_asyncio.fixture(loop_scope="session")
async def ledger(db_session) -> PendingPushRepository:
    await db_session.execute(delete(PendingPush))
    return PendingPushRepository(db_session)


async def _marks(db_session) -> dict[tuple[str, str], tuple[int, dict | None]]:
    rows = await db_session.execute(
        select(PendingPush.kind, PendingPush.key, PendingPush.seq, PendingPush.snapshot).execution_options(
            populate_existing=True
        )
    )
    return {(kind, key): (seq, snapshot) for kind, key, seq, snapshot in rows.all()}


async def _rows(db_session):
    suffix = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Ledger {suffix}", domains=[], status=TenantStatus.active)
    user = User(email=f"ledger-{suffix}@ledger.test", user_name=suffix, status=UserStatus.active)
    role = Role(name=f"ledger-{suffix}")
    permission = Permission(name=f"ledger{suffix}:read")
    db_session.add_all([tenant, user, role, permission])
    await db_session.flush()
    return tenant, user, role, permission


async def test_every_pushed_entity_is_marked_by_its_write(db_session, ledger):
    tenant, user, role, permission = await _rows(db_session)
    db_session.add_all(
        [
            RolePermission(role_id=role.id, permission_id=permission.id),
            UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id),
        ]
    )
    await db_session.flush()

    assert set(await _marks(db_session)) == {
        ("tenant", str(tenant.id)),
        ("user", str(user.id)),
        ("role", str(role.id)),
        ("permission", str(permission.id)),
        ("membership", f"{user.id}:{tenant.id}"),
    }


async def test_a_later_change_has_a_higher_seq_and_a_delivered_seq_clears_only_its_own(db_session, ledger):
    tenant, *_ = await _rows(db_session)
    key = ("tenant", str(tenant.id))
    first = (await _marks(db_session))[key][0]

    tenant.name = f"{tenant.name} renamed"
    await db_session.flush()
    second = (await _marks(db_session))[key][0]

    assert second > first
    assert await ledger.clear(*key, first) is False
    assert key in await _marks(db_session)
    assert await ledger.clear(*key, second) is True
    assert key not in await _marks(db_session)


async def test_a_write_that_changes_nothing_pushed_marks_nothing(db_session, ledger):
    tenant, *_ = await _rows(db_session)
    await db_session.execute(delete(PendingPush))
    tenant.external_org_id = "linked-by-the-adapter"
    await db_session.flush()

    assert await _marks(db_session) == {}


async def test_the_providers_state_marks_nothing_and_renews_what_is_marked(db_session, ledger):
    tenant, user, *_ = await _rows(db_session)
    await db_session.execute(delete(PendingPush).where(PendingPush.kind == "user"))
    before = (await _marks(db_session))[("tenant", str(tenant.id))][0]

    await ledger.provider_writes(True)
    tenant.name = f"{tenant.name} from keycloak"
    user.given_name = "From Keycloak"
    await db_session.flush()
    await ledger.provider_writes(False)

    marks = await _marks(db_session)
    assert ("user", str(user.id)) not in marks
    assert marks[("tenant", str(tenant.id))][0] > before


async def test_a_delete_keeps_what_its_push_needs(db_session, ledger):
    tenant, user, role, permission = await _rows(db_session)
    db_session.add(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
    await db_session.flush()
    role_id, permission_id, role_name, permission_name = role.id, permission.id, role.name, permission.name

    await db_session.delete(role)
    await db_session.delete(permission)
    await db_session.flush()

    marks = await _marks(db_session)
    assert marks[("role", str(role_id))][1] == {"name": role_name, "tenant_id": None}
    assert marks[("permission", str(permission_id))][1] == {"name": permission_name}
    assert marks[("membership", f"{user.id}:{tenant.id}")][1] == {"role_id": str(role_id)}


async def test_a_tenant_delete_keeps_its_organization_link(db_session, ledger):
    tenant, *_ = await _rows(db_session)
    tenant.external_org_id = "acme-org"
    await db_session.flush()
    tenant_id = tenant.id

    await db_session.delete(tenant)
    await db_session.flush()

    assert (await _marks(db_session))[("tenant", str(tenant_id))][1]["external_org_id"] == "acme-org"


async def test_the_keycloak_seed_links_tenants_as_the_providers_state(db_session, ledger):
    """Linking a tenant to an organization Keycloak already holds marks nothing: a push would overwrite it."""
    suffix = uuid.uuid4().hex[:10]
    provider = Provider(
        name=f"keycloak-ledger-{suffix}",
        type=ProviderType.oidc,
        issuer_url=f"http://ledger-{suffix}.localhost/realms/r",
        base_url=f"http://ledger-{suffix}.localhost/realms/r",
    )
    existing = Tenant(name=f"Existing {suffix}", domains=[], status=TenantStatus.active)
    db_session.add_all([provider, existing])
    await db_session.flush()
    await db_session.execute(delete(PendingPush))
    await ledger.provider_writes(False)

    await link_organizations_in(
        db_session,
        provider_id=provider.id,
        organizations={existing.name: f"existing-{suffix}", f"Created {suffix}": f"created-{suffix}"},
    )

    linked = (await db_session.execute(select(Tenant).where(Tenant.external_org_provider_id == provider.id))).scalars()
    assert sorted(tenant.external_org_id for tenant in linked) == [f"created-{suffix}", f"existing-{suffix}"]
    assert await _marks(db_session) == {}


async def test_the_keycloak_seed_runs_again_after_reconciliation_renamed_its_tenants(db_session, ledger):
    """A second seed finds a tenant by its link after a pass renamed it after its organization ("acme" to "Acme")."""
    suffix = uuid.uuid4().hex[:10]
    provider = Provider(
        name=f"keycloak-reseed-{suffix}",
        type=ProviderType.oidc,
        issuer_url=f"http://reseed-{suffix}.localhost/realms/r",
        base_url=f"http://reseed-{suffix}.localhost/realms/r",
    )
    db_session.add(provider)
    await db_session.flush()
    await db_session.execute(delete(PendingPush))
    await ledger.provider_writes(False)
    seeded = f"acme-{suffix}"
    organizations = {seeded: f"acme-org-{suffix}"}

    await link_organizations_in(db_session, provider_id=provider.id, organizations=organizations)
    tenant = (
        (await db_session.execute(select(Tenant).where(Tenant.external_org_provider_id == provider.id))).scalars().one()
    )
    tenant.name = f"Acme-{suffix}"
    await db_session.flush()
    await link_organizations_in(db_session, provider_id=provider.id, organizations=organizations)

    linked = (
        await db_session.execute(
            select(Tenant.id, Tenant.name, Tenant.external_org_id).where(Tenant.external_org_provider_id == provider.id)
        )
    ).all()
    assert [tuple(row) for row in linked] == [(tenant.id, f"Acme-{suffix}", f"acme-org-{suffix}")]
    assert (await db_session.execute(select(Tenant.id).where(Tenant.name == seeded))).all() == []
    assert await _marks(db_session) == {}
