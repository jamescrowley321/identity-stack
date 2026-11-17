"""KeycloakSyncAdapter against the compose Keycloak (``keycloak-test``, ``--profile keycloak``).

The adapter writes with the realm's admin client (``identity-stack-admin``, its
service account holding ``manage-users`` and ``manage-organizations``), through
the shipped ``connect_keycloak_admin``. What it wrote is read back with the
realm admin's own token. Every tenant and user is new to the test, named by a
fresh id, and removed afterwards whatever the outcome.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from expression import Ok
from fastapi import FastAPI

from app import main
from app.dependencies import identity
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import User
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.services.adapters.base import LAST_ROLE_IN_TENANT, STATUS_CHANGED
from app.services.adapters.keycloak import USER_NOT_IN_KEYCLOAK, KeycloakSyncAdapter
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    ADMIN_TIMEOUT_SECONDS,
    ISSUER_VARIABLE,
    REQUIRED_ROLES,
    KeycloakAdminError,
    KeycloakAdminRolesMissing,
    connect_keycloak_admin,
)
from app.services.role import RoleService
from tests.helpers.keycloak import KeycloakTokenSource
from tests.helpers.keycloak_model import (
    ADMIN_CLIENT_ID,
    ADMIN_CLIENT_SECRET,
    DIRECTORY_CLIENT_ID,
    DIRECTORY_CLIENT_SECRET,
)

# A fixture user's email: she signed up in Keycloak, the adapter did not create her.
FIXTURE_EMAIL = "alice@acme.test"


@pytest.fixture
def environment(monkeypatch, keycloak_token_source: KeycloakTokenSource) -> KeycloakTokenSource:
    monkeypatch.setenv(ISSUER_VARIABLE, keycloak_token_source.issuer)
    monkeypatch.setenv(ADMIN_CLIENT_ID_VARIABLE, ADMIN_CLIENT_ID)
    monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, ADMIN_CLIENT_SECRET)
    return keycloak_token_source


@pytest_asyncio.fixture(loop_scope="session")
async def sync(environment) -> AsyncIterator[KeycloakSyncAdapter]:
    async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
        yield KeycloakSyncAdapter(await connect_keycloak_admin(http))


@pytest.fixture
def realm(keycloak_token_source: KeycloakTokenSource) -> Iterator[httpx.Client]:
    """The realm admin's view of the realm, to read back what the adapter wrote."""
    with keycloak_token_source._admin() as admin:
        yield admin


@pytest.fixture
def tenant_id(realm: httpx.Client) -> Iterator[uuid.UUID]:
    tenant_id = uuid.uuid4()
    yield tenant_id
    organization = find_organization(realm, tenant_id)
    if organization is not None:
        realm.delete(f"/organizations/{organization['id']}").raise_for_status()


@pytest.fixture
def user_id(realm: httpx.Client) -> Iterator[uuid.UUID]:
    user_id = uuid.uuid4()
    yield user_id
    user = find_user(realm, user_id)
    if user is not None:
        realm.delete(f"/users/{user['id']}").raise_for_status()


@pytest.fixture
def other_user_id(realm: httpx.Client) -> Iterator[uuid.UUID]:
    other_user_id = uuid.uuid4()
    yield other_user_id
    user = find_user(realm, other_user_id)
    if user is not None:
        realm.delete(f"/users/{user['id']}").raise_for_status()


def find_organization(realm: httpx.Client, tenant_id: uuid.UUID) -> dict | None:
    response = realm.get("/organizations", params={"q": f"alias:{tenant_id}", "briefRepresentation": "false"})
    response.raise_for_status()
    found = [organization for organization in response.json() if organization["alias"] == str(tenant_id)]
    return found[0] if found else None


def find_user(realm: httpx.Client, user_id: uuid.UUID) -> dict | None:
    response = realm.get("/users", params={"username": str(user_id), "exact": "true"})
    response.raise_for_status()
    found = response.json()
    return found[0] if found else None


def members(realm: httpx.Client, organization: dict) -> list[str]:
    response = realm.get(f"/organizations/{organization['id']}/members")
    response.raise_for_status()
    return sorted(member["username"] for member in response.json())


def email_of(user_id: uuid.UUID) -> str:
    return f"{user_id.hex}@sync.test"


class TestOrganizations:
    async def test_create_update_and_delete(self, sync, realm, tenant_id):
        created = await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        organization = find_organization(realm, tenant_id)

        assert created == Ok(None)
        assert {key: organization[key] for key in ("alias", "name", "enabled")} == {
            "alias": str(tenant_id),
            "name": f"Sync {tenant_id}",
            "enabled": True,
        }

        # A grant marker the realm admin keeps on the organization survives an update.
        marker = {"type:tenant-admin": ["someone"]}
        realm.put(
            f"/organizations/{organization['id']}",
            json={"name": organization["name"], "alias": str(tenant_id), "attributes": marker},
        ).raise_for_status()
        domain = f"{tenant_id.hex}.test"
        updated = await sync.sync_tenant(
            tenant_id=tenant_id, data={"name": f"Renamed {tenant_id}", "self_provisioning_domains": [domain]}
        )
        organization = find_organization(realm, tenant_id)

        assert updated == Ok(None)
        assert (organization["name"], [entry["name"] for entry in organization["domains"]]) == (
            f"Renamed {tenant_id}",
            [domain],
        )
        assert organization["attributes"] == marker

        assert await sync.delete_tenant(tenant_id=tenant_id) == Ok(None)
        assert find_organization(realm, tenant_id) is None
        assert await sync.delete_tenant(tenant_id=tenant_id) == Ok(None)

    async def test_a_sync_keeps_what_the_realm_admin_set_beside_the_adapters_fields(self, sync, realm, tenant_id):
        """Keycloak resets the fields a PUT leaves out: a disabled organization must stay disabled."""
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        organization = find_organization(realm, tenant_id)
        kept = {"enabled": False, "description": "Set by the realm admin", "redirectUrl": "http://localhost:3000/"}
        realm.put(f"/organizations/{organization['id']}", json={**organization, **kept}).raise_for_status()

        updated = await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Renamed {tenant_id}"})
        organization = find_organization(realm, tenant_id)

        assert updated == Ok(None)
        assert {key: organization.get(key) for key in ("name", *kept)} == {"name": f"Renamed {tenant_id}", **kept}

    async def test_an_empty_domain_list_clears_the_domains(self, sync, realm, tenant_id):
        domain = f"{tenant_id.hex}.test"
        await sync.sync_tenant(
            tenant_id=tenant_id, data={"name": f"Sync {tenant_id}", "self_provisioning_domains": [domain]}
        )

        cleared = await sync.sync_tenant(
            tenant_id=tenant_id, data={"name": f"Sync {tenant_id}", "self_provisioning_domains": []}
        )

        assert cleared == Ok(None)
        assert find_organization(realm, tenant_id).get("domains", []) == []

    async def test_a_new_organization_with_no_domains_is_created(self, sync, realm, tenant_id):
        created = await sync.sync_tenant(
            tenant_id=tenant_id, data={"name": f"Sync {tenant_id}", "self_provisioning_domains": []}
        )

        assert created == Ok(None)
        assert find_organization(realm, tenant_id).get("domains", []) == []

    async def test_a_domain_another_organization_holds_is_an_error(self, sync, tenant_id):
        result = await sync.sync_tenant(
            tenant_id=tenant_id, data={"name": f"Sync {tenant_id}", "self_provisioning_domains": ["acme.test"]}
        )

        assert result.is_error()
        assert (result.error.operation, result.error.context["status"]) == ("sync_tenant", 400)


class TestUsers:
    async def test_create_disable_enable_and_delete(self, sync, realm, user_id):
        assert await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"}) == Ok(None)
        user = find_user(realm, user_id)
        assert (user["username"], user["email"], user["enabled"]) == (str(user_id), email_of(user_id), True)

        renamed = f"renamed-{email_of(user_id)}"
        deactivate = {"email": renamed, "status": "inactive", STATUS_CHANGED: True}
        assert await sync.sync_user(user_id=user_id, data=deactivate) == Ok(None)
        assert {key: find_user(realm, user_id)[key] for key in ("id", "email", "enabled")} == {
            "id": user["id"],
            "email": renamed,
            "enabled": False,
        }

        activate = {"email": renamed, "status": "active", STATUS_CHANGED: True}
        assert await sync.sync_user(user_id=user_id, data=activate) == Ok(None)
        assert find_user(realm, user_id)["enabled"] is True

        assert await sync.delete_user(user_id=user_id) == Ok(None)
        assert find_user(realm, user_id) is None
        assert await sync.delete_user(user_id=user_id) == Ok(None)

    async def test_a_status_change_for_a_user_keycloak_lacks_is_an_error_and_creates_no_user(
        self, sync, realm, user_id
    ):
        deactivated = {"email": email_of(user_id), "status": "inactive", STATUS_CHANGED: True}

        result = await sync.sync_user(user_id=user_id, data=deactivated)

        assert (result.error.operation, result.error.message) == ("sync_user", USER_NOT_IN_KEYCLOAK)
        assert find_user(realm, user_id) is None

    async def test_a_profile_update_keeps_a_realm_disabled_account_disabled(self, sync, realm, user_id):
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})
        realm.put(f"/users/{find_user(realm, user_id)['id']}", json={"enabled": False}).raise_for_status()

        renamed = f"renamed-{email_of(user_id)}"
        for email in (email_of(user_id), renamed):
            assert await sync.sync_user(user_id=user_id, data={"email": email, "status": "active"}) == Ok(None)

        assert {key: find_user(realm, user_id)[key] for key in ("email", "enabled")} == {
            "email": renamed,
            "enabled": False,
        }

    async def test_a_changed_email_is_no_longer_verified(self, sync, realm, user_id):
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})
        user = find_user(realm, user_id)
        realm.put(f"/users/{user['id']}", json={"emailVerified": True}).raise_for_status()

        assert await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"}) == Ok(None)
        assert find_user(realm, user_id)["emailVerified"] is True

        renamed = f"renamed-{email_of(user_id)}"
        assert await sync.sync_user(user_id=user_id, data={"email": renamed, "status": "active"}) == Ok(None)
        assert {key: find_user(realm, user_id)[key] for key in ("email", "emailVerified")} == {
            "email": renamed,
            "emailVerified": False,
        }

    async def test_a_user_who_signed_up_in_keycloak_is_not_taken_over_by_email(self, sync, realm, user_id):
        result = await sync.sync_user(user_id=user_id, data={"email": FIXTURE_EMAIL, "status": "active"})

        assert result.is_error()
        assert (result.error.operation, result.error.context["status"]) == ("sync_user", 409)
        assert find_user(realm, user_id) is None


class TestMembers:
    async def test_a_role_assignment_adds_and_leaving_removes(self, sync, realm, tenant_id, user_id):
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})
        organization = find_organization(realm, tenant_id)
        role_id = uuid.uuid4()

        assert await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=role_id) == Ok(None)
        assert members(realm, organization) == [str(user_id)]
        assert await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=role_id) == Ok(None)

        assert await sync.delete_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=role_id) == Ok(None)
        assert members(realm, organization) == [str(user_id)]

        left = {"email": email_of(user_id), "status": "active", "left_tenant_id": str(tenant_id)}
        assert await sync.sync_user(user_id=user_id, data=left) == Ok(None)
        assert members(realm, organization) == []
        assert await sync.sync_user(user_id=user_id, data=left) == Ok(None)

    async def test_removing_the_last_role_in_the_tenant_ends_the_membership(self, sync, realm, tenant_id, user_id):
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})
        organization = find_organization(realm, tenant_id)
        role_id = uuid.uuid4()
        await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=role_id)
        last = {"role_name": "member", LAST_ROLE_IN_TENANT: True}

        removed = await sync.delete_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=role_id, data=last)

        assert removed == Ok(None)
        assert members(realm, organization) == []
        assert await sync.delete_role_assignment(
            user_id=user_id, tenant_id=tenant_id, role_id=role_id, data=last
        ) == Ok(None)

    async def test_leaving_a_tenant_keeps_a_realm_disabled_account_disabled(self, sync, realm, tenant_id, user_id):
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})
        organization = find_organization(realm, tenant_id)
        await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())
        realm.put(f"/users/{find_user(realm, user_id)['id']}", json={"enabled": False}).raise_for_status()

        left = {"email": email_of(user_id), "status": "active", "left_tenant_id": str(tenant_id)}
        result = await sync.sync_user(user_id=user_id, data=left)

        assert result == Ok(None)
        assert members(realm, organization) == []
        assert find_user(realm, user_id)["enabled"] is False

    async def test_a_refused_profile_update_still_ends_the_membership(self, sync, realm, tenant_id, user_id):
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})
        organization = find_organization(realm, tenant_id)
        await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())

        # The fixture user holds this email, so Keycloak refuses the update.
        left = {"email": FIXTURE_EMAIL, "status": "active", "left_tenant_id": str(tenant_id)}
        result = await sync.sync_user(user_id=user_id, data=left)

        assert result.is_error()
        assert result.error.context["status"] == 409
        assert members(realm, organization) == []
        assert find_user(realm, user_id)["email"] == email_of(user_id)

    async def test_ending_a_membership_of_a_user_keycloak_lacks_is_an_error_and_creates_no_user(
        self, sync, realm, tenant_id, user_id
    ):
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        last = {"role_name": "member", LAST_ROLE_IN_TENANT: True}
        left = {"email": email_of(user_id), "status": "active", "left_tenant_id": str(tenant_id)}

        removed = await sync.delete_role_assignment(
            user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4(), data=last
        )
        leaving = await sync.sync_user(user_id=user_id, data=left)

        assert [(result.error.operation, result.error.message) for result in (removed, leaving)] == [
            ("delete_role_assignment", USER_NOT_IN_KEYCLOAK),
            ("sync_user", USER_NOT_IN_KEYCLOAK),
        ]
        assert find_user(realm, user_id) is None

    async def test_adding_to_a_tenant_not_pushed_is_an_error(self, sync, tenant_id, user_id):
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})

        result = await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())

        assert result.is_error()
        assert result.error.operation == "sync_role_assignment"


def get_organization(realm: httpx.Client, organization: dict) -> dict:
    response = realm.get(f"/organizations/{organization['id']}")
    response.raise_for_status()
    return response.json()


@pytest.fixture
def linked_account(realm: httpx.Client) -> Iterator[dict]:
    """A Keycloak account the adapter did not create, as a user who signed in through Keycloak holds one."""
    username = f"signed-up-{uuid.uuid4().hex[:12]}"
    created = realm.post("/users", json={"username": username, "email": f"{username}@sync.test", "enabled": True})
    created.raise_for_status()
    account_id = created.headers["location"].rsplit("/", 1)[1]
    yield realm.get(f"/users/{account_id}").json()
    realm.delete(f"/users/{account_id}")


def linked_to(account_id: str):
    async def linked_subject(_user_id: uuid.UUID) -> str | None:
        return account_id

    return linked_subject


class TestGrantMarkers:
    async def test_ending_a_membership_takes_the_member_out_of_the_markers_and_keeps_the_rest(
        self, sync, realm, tenant_id, user_id
    ):
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})
        organization = find_organization(realm, tenant_id)
        account_id = find_user(realm, user_id)["id"]
        await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())
        marked = {"type:tenant-admin": [account_id, "another-member"], "type:viewer": [account_id], "note": ["kept"]}
        realm.put(
            f"/organizations/{organization['id']}", json={**organization, "attributes": marked}
        ).raise_for_status()

        last = {"role_name": "member", LAST_ROLE_IN_TENANT: True}
        removed = await sync.delete_role_assignment(
            user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4(), data=last
        )
        await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())

        assert removed == Ok(None)
        assert members(realm, organization) == [str(user_id)]
        assert get_organization(realm, organization)["attributes"] == {
            "type:tenant-admin": ["another-member"],
            "note": ["kept"],
        }


class TestLinkedAccounts:
    async def test_memberships_and_status_reach_the_account_the_link_names(
        self, environment, realm, tenant_id, user_id, linked_account
    ):
        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
            sync = KeycloakSyncAdapter(
                await connect_keycloak_admin(http), linked_subject=linked_to(linked_account["id"])
            )
            await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
            organization = find_organization(realm, tenant_id)

            added = await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())
            assert (added, members(realm, organization)) == (Ok(None), [linked_account["username"]])

            disabled = await sync.sync_user(
                user_id=user_id, data={"email": email_of(user_id), "status": "inactive", STATUS_CHANGED: True}
            )
            last = {"role_name": "member", LAST_ROLE_IN_TENANT: True}
            removed = await sync.delete_role_assignment(
                user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4(), data=last
            )

        account = realm.get(f"/users/{linked_account['id']}").json()
        assert (disabled, removed) == (Ok(None), Ok(None))
        assert members(realm, organization) == []
        assert {key: account[key] for key in ("enabled", "email")} == {
            "enabled": False,
            "email": linked_account["email"],
        }
        assert find_user(realm, user_id) is None

    async def test_a_profile_update_of_a_linked_user_creates_no_second_account(
        self, environment, realm, user_id, linked_account
    ):
        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
            sync = KeycloakSyncAdapter(
                await connect_keycloak_admin(http), linked_subject=linked_to(linked_account["id"])
            )
            updated = await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})

        account = realm.get(f"/users/{linked_account['id']}").json()
        assert (updated, find_user(realm, user_id)) == (Ok(None), None)
        assert account["email"] == linked_account["email"]


class TestDeletingARole:
    """RoleService over Postgres and the compose Keycloak: a role delete ends the memberships it held alone."""

    async def test_deleting_a_users_only_role_ends_the_membership_and_another_role_keeps_one(
        self, db_session, sync, realm, tenant_id, user_id, other_user_id
    ):
        db_session.add(Tenant(id=tenant_id, name=f"Sync {tenant_id}"))
        db_session.add_all([User(id=uid, email=email_of(uid), user_name=str(uid)) for uid in (user_id, other_user_id)])
        await db_session.flush()
        deleted, kept = Role(name=f"deleted-{tenant_id.hex}"), Role(name=f"kept-{tenant_id.hex}")
        db_session.add_all([deleted, kept])
        await db_session.flush()
        db_session.add_all(
            [
                UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=deleted.id),
                UserTenantRole(user_id=other_user_id, tenant_id=tenant_id, role_id=deleted.id),
                UserTenantRole(user_id=other_user_id, tenant_id=tenant_id, role_id=kept.id),
            ]
        )
        await db_session.flush()
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        for uid in (user_id, other_user_id):
            await sync.sync_user(user_id=uid, data={"email": email_of(uid), "status": "active"})
            await sync.sync_role_assignment(user_id=uid, tenant_id=tenant_id, role_id=deleted.id)
        organization = find_organization(realm, tenant_id)
        service = RoleService(
            repository=RoleRepository(db_session),
            permission_repository=PermissionRepository(db_session),
            assignment_repository=UserTenantRoleRepository(db_session),
            adapter=sync,
            publisher=AsyncMock(),
        )

        result = await service.delete_role(role_id=deleted.id)

        assert result == Ok({"status": "deleted", "name": deleted.name})
        assert members(realm, organization) == [str(other_user_id)]


class TestCredential:
    async def test_a_wrong_secret_is_refused(self, environment, monkeypatch):
        monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, "wrong")

        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
            with pytest.raises(KeycloakAdminError) as refused:
                await connect_keycloak_admin(http)

        assert refused.value.status == 401

    async def test_the_read_only_directory_client_is_refused_at_startup(self, environment, monkeypatch):
        """Keycloak accepts its credential and lets it read; it would fail only on the first write."""
        monkeypatch.setenv(ADMIN_CLIENT_ID_VARIABLE, DIRECTORY_CLIENT_ID)
        monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, DIRECTORY_CLIENT_SECRET)

        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
            with pytest.raises(KeycloakAdminRolesMissing) as refused:
                await connect_keycloak_admin(http)

        assert (refused.value.status, refused.value.missing) == (403, REQUIRED_ROLES)

    async def test_startup_stops_on_the_read_only_directory_client(self, environment, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        monkeypatch.setenv(ADMIN_CLIENT_ID_VARIABLE, DIRECTORY_CLIENT_ID)
        monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, DIRECTORY_CLIENT_SECRET)

        with pytest.raises(RuntimeError, match="lacks realm-management roles"):
            await main._start_sync_provider(FastAPI())

    async def test_startup_opens_the_adapter_with_a_working_credential(self, environment, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        app = FastAPI()

        http = await main._start_sync_provider(app)
        try:
            assert app.state.keycloak_admin.settings.client_id == ADMIN_CLIENT_ID
        finally:
            await http.aclose()

    async def test_startup_stops_on_a_wrong_secret(self, environment, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, "wrong")

        with pytest.raises(RuntimeError, match="status 401"):
            await main._start_sync_provider(FastAPI())
