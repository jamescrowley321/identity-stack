"""ZitadelSyncAdapter against the compose Zitadel and the test Postgres, through the real services.

A tenant created through TenantService gets an organization closed to self-registration,
granted the project, and linked from the tenant row. A user created in that tenant through
UserService is a human user in that organization, linked by IdP link; deactivating and
activating them follows in Zitadel, and delete_user removes them. With Zitadel unreachable
every call answers Error(SyncError).
"""

import uuid

import httpx

from app.dependencies.zitadel_sync import ZitadelAdminSettings, build_zitadel_sync_adapter
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.adapters.base import CREATED_IN_TENANT, PROVIDER_SYNC, SyncError
from app.services.adapters.zitadel import ZitadelSyncAdapter
from app.services.tenant import TenantService
from app.services.user import UserService

ACTIVE = "USER_STATE_ACTIVE"
INACTIVE = "USER_STATE_INACTIVE"


async def _zitadel_provider(session, issuer: str) -> uuid.UUID:
    """The ``zitadel`` provider row, as ``scripts/seed_zitadel.py`` writes it."""
    provider = Provider(
        name="zitadel", type=ProviderType.oidc, issuer_url=issuer, base_url=issuer, capabilities=["sso"]
    )
    session.add(provider)
    await session.flush()
    return provider.id


def _adapter(settings: ZitadelAdminSettings, session) -> ZitadelSyncAdapter:
    return build_zitadel_sync_adapter(settings, session)


async def _tenant(session, settings: ZitadelAdminSettings, name: str) -> uuid.UUID:
    created = await TenantService(
        repository=TenantRepository(session), adapter=_adapter(settings, session)
    ).create_tenant(name=name)
    assert created.is_ok(), created
    return uuid.UUID(str(created.ok["id"]))


def _unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def _user_state(zitadel: httpx.Client, user_id: uuid.UUID) -> dict:
    response = zitadel.get(f"/v2/users/{user_id}")
    response.raise_for_status()
    return response.json()["user"]


async def test_a_created_tenant_gets_an_organization_closed_to_registration_and_granted_the_project(
    db_session, zitadel_settings, zitadel
):
    provider_id = await _zitadel_provider(db_session, zitadel_settings.base_url)
    name = _unique("Initech")

    tenant_id = await _tenant(db_session, zitadel_settings, name)

    db_session.expire_all()
    tenant = await TenantRepository(db_session).get(tenant_id)
    assert tenant.external_org_provider_id == provider_id
    org_id = tenant.external_org_id
    # The organization's id is the tenant's, so a second sync of the tenant finds it.
    assert org_id == str(tenant_id)
    found = zitadel.post("/v2/organizations/_search", json={"queries": [{"idQuery": {"id": org_id}}]}).json()
    assert [(org["id"], org["name"]) for org in found["result"]] == [(org_id, name)]

    settings = zitadel.get("/v2/settings/login", params={"ctx.orgId": org_id}).json()["settings"]
    assert settings["resourceOwnerType"] == "RESOURCE_OWNER_TYPE_ORG"
    assert settings.get("allowRegister", False) is False
    # The policy copied the default's other values: password login still works.
    assert settings.get("allowUsernamePassword") is True
    # And Login V2 refuses a self-registration into the organization.
    register = httpx.get(
        f"{zitadel_settings.base_url}/ui/v2/login/register",
        params={"organization": org_id},
        follow_redirects=True,
        timeout=15.0,
    )
    assert register.status_code == 200
    assert 'data-testid="email-text-input"' not in register.text
    assert "The registration is disabled" in register.text

    grants = zitadel.post(f"/management/v1/projects/{zitadel_settings.project_id}/grants/_search", json={}).json()[
        "result"
    ]
    granted = {grant["grantedOrgId"]: sorted(grant.get("grantedRoleKeys", [])) for grant in grants}
    assert granted[org_id] == ["admin", "member", "owner", "viewer"]


async def test_a_user_created_in_the_tenant_is_a_human_user_in_its_organization_and_follows_its_status(
    db_session, zitadel_settings, zitadel
):
    provider_id = await _zitadel_provider(db_session, zitadel_settings.base_url)
    tenant_id = await _tenant(db_session, zitadel_settings, _unique("Hooli"))
    db_session.expire_all()
    org_id = (await TenantRepository(db_session).get(tenant_id)).external_org_id
    email = f"{uuid.uuid4().hex[:8]}@hooli.test"
    users = UserService(
        repository=UserRepository(db_session),
        adapter=_adapter(zitadel_settings, db_session),
        assignment_repository=UserTenantRoleRepository(db_session),
    )

    created = await users.create_user(
        tenant_id=tenant_id, email=email, user_name="gavin", given_name="Gavin", family_name=""
    )

    assert created.is_ok(), created
    user_id = uuid.UUID(str(created.ok["id"]))
    zitadel_user = _user_state(zitadel, user_id)
    assert zitadel_user["details"]["resourceOwner"] == org_id
    assert zitadel_user["state"] == ACTIVE
    # Unverified: a tenant admin typed the email in, and Zitadel has the user prove it.
    assert zitadel_user["human"]["email"]["email"] == email
    assert zitadel_user["human"]["email"].get("isVerified", False) is False
    assert (zitadel_user["human"]["profile"]["givenName"], zitadel_user["human"]["profile"]["familyName"]) == (
        "Gavin",
        "gavin",
    )
    link = await IdPLinkRepository(db_session).get_by_provider_and_sub(provider_id, str(user_id))
    assert link.user_id == user_id

    # The status routes need a membership in the tenant.
    role = Role(name=f"member-{uuid.uuid4().hex[:8]}")
    db_session.add(role)
    await db_session.flush()
    db_session.add(UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=role.id))
    await db_session.flush()

    assert (await users.deactivate_user(tenant_id=tenant_id, user_id=user_id)).is_ok()
    assert _user_state(zitadel, user_id)["state"] == INACTIVE

    assert (await users.activate_user(tenant_id=tenant_id, user_id=user_id)).is_ok()
    assert _user_state(zitadel, user_id)["state"] == ACTIVE

    adapter = _adapter(zitadel_settings, db_session)
    assert (await adapter.delete_user(user_id=user_id)).is_ok()
    assert zitadel.get(f"/v2/users/{user_id}").status_code == 404
    # Deleting again: Zitadel no longer has the user, which counts as removed.
    assert (await adapter.delete_user(user_id=user_id)).is_ok()


async def test_with_zitadel_unreachable_every_call_answers_a_sync_error(db_session, zitadel_settings):
    await _zitadel_provider(db_session, zitadel_settings.base_url)
    tenant_id = await _tenant(db_session, zitadel_settings, _unique("Pied Piper"))
    unreachable = ZitadelAdminSettings(
        base_url="http://127.0.0.1:9", project_id=zitadel_settings.project_id, token="unused", timeout_seconds=2.0
    )
    adapter = _adapter(unreachable, db_session)

    results = {
        "sync_tenant": await adapter.sync_tenant(tenant_id=uuid.uuid4(), data={"name": _unique("Raviga")}),
        "sync_user": await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "r@raviga.test", "status": "active", "user_name": "r", CREATED_IN_TENANT: str(tenant_id)},
        ),
        "delete_user": await adapter.delete_user(user_id=uuid.uuid4()),
    }

    assert {operation: type(result.error) for operation, result in results.items() if result.is_error()} == {
        "sync_tenant": SyncError,
        "sync_user": SyncError,
        "delete_user": SyncError,
    }


async def test_the_owners_own_names_reach_their_zitadel_user_and_change_nothing_else(
    db_session, zitadel_settings, zitadel
):
    """UserService.update_own_names with the caller's sub (OWN_PROFILE_OF): the profile's names save."""
    await _zitadel_provider(db_session, zitadel_settings.base_url)
    tenant_id = await _tenant(db_session, zitadel_settings, _unique("Pied Piper"))
    email = f"{uuid.uuid4().hex[:8]}@piedpiper.test"
    users = UserService(
        repository=UserRepository(db_session),
        adapter=_adapter(zitadel_settings, db_session),
        assignment_repository=UserTenantRoleRepository(db_session),
    )
    created = await users.create_user(
        tenant_id=tenant_id, email=email, user_name="richard", given_name="Richard", family_name="Hendricks"
    )
    assert created.is_ok(), created
    user_id = uuid.UUID(str(created.ok["id"]))

    renamed = await users.update_own_names(user_id=user_id, given_name="Rich", family_name="H", subject=str(user_id))

    assert renamed.is_ok(), renamed
    assert PROVIDER_SYNC not in renamed.ok
    zitadel_user = _user_state(zitadel, user_id)
    assert (
        zitadel_user["human"]["profile"]["givenName"],
        zitadel_user["human"]["profile"]["familyName"],
        zitadel_user["human"]["email"]["email"],
        zitadel_user["state"],
    ) == ("Rich", "H", email, ACTIVE)
