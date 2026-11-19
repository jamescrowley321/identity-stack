"""KeycloakSyncAdapter against the compose Keycloak (``keycloak-test``, ``--profile keycloak``).

The adapter writes with the realm's admin client (``identity-stack-admin``), through
the shipped ``connect_keycloak_admin``. What it wrote is read back with the realm
admin's own token. Each canonical operation of the mapping table
(``docs/keycloak.md#admin-adapter``) is pushed, checked in Keycloak, and pushed
again: the repeat must leave no admin event of the adapter's client, so it changed
nothing. Every tenant, user and client role is new to the test, named by a fresh
id, and removed afterwards whatever the outcome.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from expression import Ok
from fastapi import FastAPI
from sqlalchemy import select

from app import main
from app.dependencies import identity
from app.dependencies.keycloak_sync import RepositoryKeycloakLinks
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import LINK_ORIGIN, LINKED_BY_BACKEND, IdPLink, User
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.services.adapters.base import (
    CREATED_IN_TENANT,
    LAST_ROLE_IN_TENANT,
    OWN_PROFILE_OF,
    STATUS_CHANGED,
    TenantAttributes,
)
from app.services.adapters.keycloak import (
    PERMISSION_ATTRIBUTE,
    PROVIDER_NAME,
    ROLE_ATTRIBUTE,
    KeycloakSyncAdapter,
    Refusal,
)
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    ADMIN_TIMEOUT_SECONDS,
    API_CLIENT_VARIABLE,
    ISSUER_VARIABLE,
    KeycloakAdminError,
    connect_keycloak_admin,
)
from app.services.role import RoleService
from scripts.seed_keycloak import KeycloakAccount as SeededAccount
from scripts.seed_keycloak import linked_user
from tests.helpers.identity_fixture import IDENTITIES
from tests.helpers.keycloak import KeycloakTokenSource, decode_unverified
from tests.helpers.keycloak_links import MemoryKeycloakLinks
from tests.helpers.keycloak_model import ADMIN_CLIENT_ID, ADMIN_CLIENT_SECRET, API_AUDIENCE, SPA_CLIENT_ID
from tests.helpers.keycloak_target import KeycloakUser

# A fixture user's email: she signed up in Keycloak, the adapter did not create her.
FIXTURE_EMAIL = IDENTITIES.cases.member.email


@pytest.fixture
def environment(monkeypatch, keycloak_token_source: KeycloakTokenSource) -> KeycloakTokenSource:
    monkeypatch.setenv(ISSUER_VARIABLE, keycloak_token_source.issuer)
    monkeypatch.setenv(ADMIN_CLIENT_ID_VARIABLE, ADMIN_CLIENT_ID)
    monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, ADMIN_CLIENT_SECRET)
    monkeypatch.setenv(API_CLIENT_VARIABLE, API_AUDIENCE)
    return keycloak_token_source


@pytest.fixture
def links() -> MemoryKeycloakLinks:
    return MemoryKeycloakLinks()


@pytest_asyncio.fixture(loop_scope="session")
async def sync(environment, links) -> AsyncIterator[KeycloakSyncAdapter]:
    async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
        yield KeycloakSyncAdapter(await connect_keycloak_admin(http), links)


@pytest.fixture
def realm(keycloak_token_source: KeycloakTokenSource) -> Iterator[httpx.Client]:
    """The realm admin's view of the realm, to read back what the adapter wrote."""
    with keycloak_token_source._admin() as admin:
        yield admin


def _cleanup_users(realm: httpx.Client, user_ids: list[uuid.UUID]) -> None:
    for user_id in user_ids:
        user = find_user(realm, user_id)
        if user is not None:
            realm.delete(f"/users/{user['id']}").raise_for_status()


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
    _cleanup_users(realm, [user_id])


@pytest.fixture
def other_user_id(realm: httpx.Client) -> Iterator[uuid.UUID]:
    other_user_id = uuid.uuid4()
    yield other_user_id
    _cleanup_users(realm, [other_user_id])


@pytest.fixture
def role_id(realm: httpx.Client) -> Iterator[uuid.UUID]:
    role_id = uuid.uuid4()
    yield role_id
    _cleanup_client_roles(realm, ROLE_ATTRIBUTE, role_id)


@pytest.fixture
def permission_id(realm: httpx.Client) -> Iterator[uuid.UUID]:
    permission_id = uuid.uuid4()
    yield permission_id
    _cleanup_client_roles(realm, PERMISSION_ATTRIBUTE, permission_id)


def _cleanup_client_roles(realm: httpx.Client, attribute: str, canonical_id: uuid.UUID) -> None:
    for role in client_roles(realm):
        if str(canonical_id) in (role.get("attributes") or {}).get(attribute, []):
            realm.delete(f"/roles-by-id/{role['id']}").raise_for_status()


def find_organization(realm: httpx.Client, alias: object) -> dict | None:
    response = realm.get("/organizations", params={"q": f"alias:{alias}", "briefRepresentation": "false"})
    response.raise_for_status()
    found = [organization for organization in response.json() if organization["alias"] == str(alias)]
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


def groups(realm: httpx.Client, organization: dict) -> dict[str, dict]:
    """The organization's groups by name, each with its attributes and its members' usernames."""
    response = realm.get(f"/organizations/{organization['id']}/groups", params={"briefRepresentation": "false"})
    response.raise_for_status()
    found = {}
    for group in response.json():
        held = realm.get(f"/organizations/{organization['id']}/groups/{group['id']}/members")
        held.raise_for_status()
        mappings = realm.get(f"/organizations/{organization['id']}/groups/{group['id']}/role-mappings")
        mappings.raise_for_status()
        found[group["name"]] = {
            "attributes": group.get("attributes", {}),
            "members": sorted(member["username"] for member in held.json()),
            "role_mappings": mappings.json(),
        }
    return found


def api_client(realm: httpx.Client) -> str:
    response = realm.get("/clients", params={"clientId": API_AUDIENCE})
    response.raise_for_status()
    return response.json()[0]["id"]


def client_roles(realm: httpx.Client) -> list[dict]:
    response = realm.get(f"/clients/{api_client(realm)}/roles", params={"briefRepresentation": "false"})
    response.raise_for_status()
    return response.json()


def client_role(realm: httpx.Client, attribute: str, canonical_id: uuid.UUID) -> dict | None:
    found = [
        role for role in client_roles(realm) if str(canonical_id) in (role.get("attributes") or {}).get(attribute, [])
    ]
    return found[0] if found else None


def composites(realm: httpx.Client, role: dict) -> list[str]:
    response = realm.get(f"/roles-by-id/{role['id']}/composites")
    response.raise_for_status()
    return sorted(entry["name"] for entry in response.json())


def email_of(user_id: uuid.UUID) -> str:
    return f"{user_id.hex}@sync.test"


def last_adapter_event(realm: httpx.Client) -> dict | None:
    """The newest admin event the adapter's client made: unchanged after a push means the push wrote nothing."""
    client = realm.get("/clients", params={"clientId": ADMIN_CLIENT_ID})
    client.raise_for_status()
    response = realm.get("/admin-events", params={"authClient": client.json()[0]["id"], "first": "0", "max": "1"})
    response.raise_for_status()
    found = response.json()
    return found[0] if found else None


async def repeat_writes_nothing(realm: httpx.Client, push) -> None:
    """Push again: Ok, and no admin event of the adapter's client since the first push."""
    before = last_adapter_event(realm)
    # The push being repeated was recorded: the realm keeps the adapter's admin events.
    assert before is not None
    assert await push() == Ok(None)
    assert last_adapter_event(realm) == before


class TestTenants:
    async def test_create_update_status_and_delete(self, sync, realm, links, tenant_id):
        domain = f"{tenant_id.hex}.test"
        data = {"name": f"Sync {tenant_id}", "self_provisioning_domains": [domain]}

        assert await sync.sync_tenant(tenant_id=tenant_id, data=data) == Ok(None)
        organization = find_organization(realm, tenant_id)
        assert {key: organization[key] for key in ("alias", "name", "enabled")} == {
            "alias": str(tenant_id),
            "name": f"Sync {tenant_id}",
            "enabled": True,
        }
        assert [entry["name"] for entry in organization["domains"]] == [domain]
        assert links.aliases == {tenant_id: str(tenant_id)}
        await repeat_writes_nothing(realm, lambda: sync.sync_tenant(tenant_id=tenant_id, data=data))

        suspended = {"name": f"Renamed {tenant_id}", "self_provisioning_domains": [], "status": "suspended"}
        assert await sync.sync_tenant(tenant_id=tenant_id, data=suspended) == Ok(None)
        organization = find_organization(realm, tenant_id)
        assert (organization["name"], organization.get("domains", []), organization["enabled"]) == (
            f"Renamed {tenant_id}",
            [],
            False,
        )
        await repeat_writes_nothing(realm, lambda: sync.sync_tenant(tenant_id=tenant_id, data=suspended))

        assert await sync.delete_tenant(tenant_id=tenant_id) == Ok(None)
        assert find_organization(realm, tenant_id) is None
        await repeat_writes_nothing(realm, lambda: sync.delete_tenant(tenant_id=tenant_id))

    async def test_a_sync_keeps_what_the_realm_admin_set_beside_the_adapters_fields(self, sync, realm, tenant_id):
        """Keycloak resets the fields a PUT leaves out: a description and redirect URL must stay."""
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        organization = find_organization(realm, tenant_id)
        kept = {"enabled": False, "description": "Set by the realm admin", "redirectUrl": "http://localhost:3000/"}
        realm.put(f"/organizations/{organization['id']}", json={**organization, **kept}).raise_for_status()

        updated = await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Renamed {tenant_id}"})
        organization = find_organization(realm, tenant_id)

        assert updated == Ok(None)
        assert {key: organization.get(key) for key in ("name", *kept)} == {"name": f"Renamed {tenant_id}", **kept}

    async def test_a_domain_another_organization_holds_is_an_error(self, sync, tenant_id):
        result = await sync.sync_tenant(
            tenant_id=tenant_id, data={"name": f"Sync {tenant_id}", "self_provisioning_domains": ["acme.test"]}
        )

        assert (result.error.operation, result.error.context["status"]) == ("sync_tenant", 400)


class TestUsers:
    async def test_create_update_status_and_delete(self, sync, realm, links, tenant_id, user_id):
        created = {
            "email": email_of(user_id),
            "status": "active",
            "given_name": "Sync",
            "family_name": "User",
            CREATED_IN_TENANT: str(tenant_id),
        }

        assert await sync.sync_user(user_id=user_id, data=created) == Ok(None)
        user = find_user(realm, user_id)
        assert {key: user[key] for key in ("username", "email", "firstName", "lastName", "enabled")} == {
            "username": str(user_id),
            "email": email_of(user_id),
            "firstName": "Sync",
            "lastName": "User",
            "enabled": True,
        }
        assert links.linked_users == {user_id: (user["id"], email_of(user_id))}
        await repeat_writes_nothing(realm, lambda: sync.sync_user(user_id=user_id, data=created))

        renamed = {"email": f"renamed-{email_of(user_id)}", "status": "active", "given_name": "Renamed"}
        assert await sync.sync_user(user_id=user_id, data=renamed) == Ok(None)
        assert {key: find_user(realm, user_id)[key] for key in ("email", "emailVerified", "firstName")} == {
            "email": renamed["email"],
            "emailVerified": False,
            "firstName": "Renamed",
        }
        await repeat_writes_nothing(realm, lambda: sync.sync_user(user_id=user_id, data=renamed))

        deactivated = {**renamed, "status": "inactive", STATUS_CHANGED: True}
        assert await sync.sync_user(user_id=user_id, data=deactivated) == Ok(None)
        assert find_user(realm, user_id)["enabled"] is False
        await repeat_writes_nothing(realm, lambda: sync.sync_user(user_id=user_id, data=deactivated))

        assert await sync.delete_user(user_id=user_id) == Ok(None)
        assert find_user(realm, user_id) is None
        await repeat_writes_nothing(realm, lambda: sync.delete_user(user_id=user_id))

    async def test_a_profile_update_keeps_a_realm_disabled_account_disabled(self, sync, realm, user_id):
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})
        realm.put(f"/users/{find_user(realm, user_id)['id']}", json={"enabled": False}).raise_for_status()

        renamed = f"renamed-{email_of(user_id)}"
        assert await sync.sync_user(user_id=user_id, data={"email": renamed, "status": "active"}) == Ok(None)

        assert {key: find_user(realm, user_id)[key] for key in ("email", "enabled")} == {
            "email": renamed,
            "enabled": False,
        }

    async def test_a_status_change_for_a_user_keycloak_lacks_is_an_error_and_creates_no_user(
        self, sync, realm, user_id
    ):
        deactivated = {"email": email_of(user_id), "status": "inactive", STATUS_CHANGED: True}

        result = await sync.sync_user(user_id=user_id, data=deactivated)

        assert (result.error.operation, result.error.context["refusal"]) == (
            "sync_user",
            Refusal.USER_NOT_IN_KEYCLOAK,
        )
        assert find_user(realm, user_id) is None

    async def test_a_user_who_signed_up_in_keycloak_is_not_taken_over_by_email(self, sync, realm, user_id):
        result = await sync.sync_user(user_id=user_id, data={"email": FIXTURE_EMAIL, "status": "active"})

        assert (result.error.operation, result.error.context["status"]) == ("sync_user", 409)
        assert find_user(realm, user_id) is None


async def push_role(sync, role_id, permission_id, *, role_name: str, permission_name: str):
    """A permission and a global role that is its composite, as PermissionService and RoleService push them."""
    assert await sync.sync_permission(permission_id=permission_id, data={"name": permission_name}) == Ok(None)
    role = {"name": role_name, "description": "Synced", "tenant_id": None, "permission_names": [permission_name]}
    assert await sync.sync_role(role_id=role_id, data=role) == Ok(None)
    return role


class TestCustomAttributes:
    """The custom_attributes capability: user attributes and organization attributes round-trip."""

    async def test_a_users_attributes_round_trip_and_keep_its_profile(self, sync, realm, user_id):
        await sync.sync_user(
            user_id=user_id,
            data={"email": email_of(user_id), "status": "active", "given_name": "Attr", "family_name": "User"},
        )
        subject = find_user(realm, user_id)["id"]

        assert await sync.set_user_attribute(subject=subject, key="department", value="Engineering") == Ok(None)
        assert await sync.set_user_attribute(subject=subject, key="job_title", value="Lead") == Ok(None)
        assert await sync.user_attributes(subject=subject) == Ok({"department": "Engineering", "job_title": "Lead"})

        assert await sync.set_user_attribute(subject=subject, key="job_title", value=None) == Ok(None)
        assert await sync.user_attributes(subject=subject) == Ok({"department": "Engineering"})
        user = find_user(realm, user_id)
        assert {key: user.get(key) for key in ("email", "firstName", "lastName", "enabled")} == {
            "email": email_of(user_id),
            "firstName": "Attr",
            "lastName": "User",
            "enabled": True,
        }

    async def test_an_organizations_attributes_round_trip_and_keep_its_fields(self, sync, realm, links, tenant_id):
        domain = f"{tenant_id.hex}.test"
        await sync.sync_tenant(
            tenant_id=tenant_id, data={"name": f"Attr {tenant_id}", "self_provisioning_domains": [domain]}
        )
        tenant = str(tenant_id)

        written = await sync.set_tenant_attributes(tenant=tenant, attributes={"plan_tier": "pro", "max_members": 25})
        assert written == Ok(None)
        assert await sync.tenant_attributes(tenant=tenant) == Ok(
            TenantAttributes(f"Attr {tenant_id}", {"plan_tier": "pro", "max_members": 25})
        )

        assert await sync.set_tenant_attributes(tenant=tenant, attributes={"plan_tier": None}) == Ok(None)
        assert await sync.tenant_attributes(tenant=tenant) == Ok(
            TenantAttributes(f"Attr {tenant_id}", {"max_members": 25})
        )
        organization = find_organization(realm, tenant_id)
        domains = [entry["name"] for entry in organization["domains"]]
        assert (organization["name"], domains, organization["enabled"]) == (f"Attr {tenant_id}", [domain], True)
        # A later sync of the tenant keeps the attributes.
        assert await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Renamed {tenant_id}"}) == Ok(None)
        assert await sync.tenant_attributes(tenant=tenant) == Ok(
            TenantAttributes(f"Renamed {tenant_id}", {"max_members": 25})
        )

    async def test_a_tenant_without_an_organization_has_no_attributes(self, sync, tenant_id):
        assert await sync.tenant_attributes(tenant=str(tenant_id)) == Ok(None)


class TestOwnNames:
    """``OWN_PROFILE_OF``: a profile's names reach the owner's own account, one they signed up with too."""

    @pytest.fixture
    def account(self, realm: httpx.Client) -> Iterator[dict]:
        """An account whose username is not a canonical id: one its owner signed up with."""
        username = f"own-{uuid.uuid4().hex[:8]}"
        email = f"{username}@sync.test"
        realm.post(
            "/users",
            json={
                "username": username,
                "email": email,
                "emailVerified": True,
                "enabled": True,
                "firstName": "Before",
                "lastName": "Rename",
            },
        ).raise_for_status()
        found = realm.get("/users", params={"username": username, "exact": "true"})
        found.raise_for_status()
        account = found.json()[0]
        yield account
        realm.delete(f"/users/{account['id']}").raise_for_status()

    async def test_the_names_reach_the_account_and_its_email_and_status_stay(self, sync, realm, user_id, account):
        data = {
            "email": "canonical-other@sync.test",
            "status": "inactive",
            "given_name": "After",
            "family_name": "Renamed",
            OWN_PROFILE_OF: account["id"],
        }

        assert await sync.sync_user(user_id=user_id, data=data) == Ok(None)

        held = realm.get(f"/users/{account['id']}").json()
        assert {key: held.get(key) for key in ("firstName", "lastName", "email", "emailVerified", "enabled")} == {
            "firstName": "After",
            "lastName": "Renamed",
            "email": account["email"],
            "emailVerified": True,
            "enabled": True,
        }
        await repeat_writes_nothing(realm, lambda: sync.sync_user(user_id=user_id, data=data))
        # No account was made under the canonical id.
        assert find_user(realm, user_id) is None


class TestPermissionsAndRoles:
    async def test_permissions_are_api_client_roles_and_roles_their_composites(
        self, sync, realm, role_id, permission_id
    ):
        permission_name = f"sync{permission_id.hex[:8]}:read"
        role_name = f"sync-{role_id.hex[:8]}"
        role = await push_role(sync, role_id, permission_id, role_name=role_name, permission_name=permission_name)

        pushed_permission = client_role(realm, PERMISSION_ATTRIBUTE, permission_id)
        pushed_role = client_role(realm, ROLE_ATTRIBUTE, role_id)
        assert (pushed_permission["name"], pushed_role["name"], pushed_role["description"]) == (
            permission_name,
            role_name,
            "Synced",
        )
        assert composites(realm, pushed_role) == [permission_name]
        await repeat_writes_nothing(realm, lambda: sync.sync_role(role_id=role_id, data=role))
        await repeat_writes_nothing(
            realm, lambda: sync.sync_permission(permission_id=permission_id, data={"name": permission_name})
        )

        renamed_permission = f"{permission_name}x"
        assert await sync.sync_permission(
            permission_id=permission_id, data={"name": renamed_permission, "previous_name": permission_name}
        ) == Ok(None)
        assert composites(realm, pushed_role) == [renamed_permission]

        emptied = {**role, "permission_names": []}
        assert await sync.sync_role(role_id=role_id, data=emptied) == Ok(None)
        assert composites(realm, pushed_role) == []
        await repeat_writes_nothing(realm, lambda: sync.sync_role(role_id=role_id, data=emptied))

        assert await sync.delete_permission(permission_id=permission_id, data={"name": renamed_permission}) == Ok(None)
        assert client_role(realm, PERMISSION_ATTRIBUTE, permission_id) is None
        await repeat_writes_nothing(
            realm, lambda: sync.delete_permission(permission_id=permission_id, data={"name": renamed_permission})
        )

    async def test_a_role_rename_renames_its_groups_and_a_delete_removes_them(
        self, sync, realm, tenant_id, user_id, role_id, permission_id
    ):
        role_name = f"sync-{role_id.hex[:8]}"
        role = await push_role(
            sync, role_id, permission_id, role_name=role_name, permission_name=f"sync{permission_id.hex[:8]}:read"
        )
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})
        assert await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=role_id) == Ok(None)
        organization = find_organization(realm, tenant_id)

        renamed = {**role, "name": f"{role_name}-renamed", "previous_name": role_name}
        assert await sync.sync_role(role_id=role_id, data=renamed) == Ok(None)
        assert client_role(realm, ROLE_ATTRIBUTE, role_id)["name"] == f"{role_name}-renamed"
        assert {name: group["members"] for name, group in groups(realm, organization).items()} == {
            f"{role_name}-renamed": [str(user_id)]
        }

        assert await sync.delete_role(role_id=role_id, data={"name": f"{role_name}-renamed", "tenant_id": None}) == Ok(
            None
        )
        assert (client_role(realm, ROLE_ATTRIBUTE, role_id), groups(realm, organization)) == (None, {})
        await repeat_writes_nothing(
            realm, lambda: sync.delete_role(role_id=role_id, data={"name": f"{role_name}-renamed", "tenant_id": None})
        )


class TestRoleAssignments:
    async def test_assign_unassign_and_end_the_membership(
        self, sync, realm, tenant_id, user_id, role_id, permission_id
    ):
        role_name = f"sync-{role_id.hex[:8]}"
        await push_role(
            sync, role_id, permission_id, role_name=role_name, permission_name=f"sync{permission_id.hex[:8]}:r"
        )
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})
        organization = find_organization(realm, tenant_id)
        assignment = {"user_id": user_id, "tenant_id": tenant_id, "role_id": role_id}

        assert await sync.sync_role_assignment(**assignment, data={"role_name": role_name}) == Ok(None)
        assert members(realm, organization) == [str(user_id)]
        assert groups(realm, organization) == {
            role_name: {"attributes": {ROLE_ATTRIBUTE: [str(role_id)]}, "members": [str(user_id)], "role_mappings": {}}
        }
        await repeat_writes_nothing(realm, lambda: sync.sync_role_assignment(**assignment))

        unassigned = {"role_name": role_name}
        assert await sync.delete_role_assignment(**assignment, data=unassigned) == Ok(None)
        assert (members(realm, organization), groups(realm, organization)[role_name]["members"]) == (
            [str(user_id)],
            [],
        )
        await repeat_writes_nothing(realm, lambda: sync.delete_role_assignment(**assignment, data=unassigned))

        await sync.sync_role_assignment(**assignment)
        last = {"role_name": role_name, LAST_ROLE_IN_TENANT: True}
        assert await sync.delete_role_assignment(**assignment, data=last) == Ok(None)
        assert (members(realm, organization), groups(realm, organization)[role_name]["members"]) == ([], [])
        await repeat_writes_nothing(realm, lambda: sync.delete_role_assignment(**assignment, data=last))

    async def test_leaving_a_tenant_ends_the_membership_and_keeps_a_realm_disabled_account_disabled(
        self, sync, realm, tenant_id, user_id, role_id, permission_id
    ):
        await push_role(
            sync,
            role_id,
            permission_id,
            role_name=f"sync-{role_id.hex[:8]}",
            permission_name=f"s{permission_id.hex[:8]}:r",
        )
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})
        await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=role_id)
        organization = find_organization(realm, tenant_id)
        realm.put(f"/users/{find_user(realm, user_id)['id']}", json={"enabled": False}).raise_for_status()

        left = {"email": email_of(user_id), "status": "active", "left_tenant_id": str(tenant_id)}
        assert await sync.sync_user(user_id=user_id, data=left) == Ok(None)

        assert (members(realm, organization), find_user(realm, user_id)["enabled"]) == ([], False)
        await repeat_writes_nothing(realm, lambda: sync.sync_user(user_id=user_id, data=left))

    async def test_a_role_not_pushed_is_an_error_and_adds_nothing(self, sync, realm, tenant_id, user_id):
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})

        result = await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())

        assert result.error.context["refusal"] == Refusal.ROLE_NOT_IN_KEYCLOAK
        assert members(realm, find_organization(realm, tenant_id)) == []

    async def test_a_token_issued_after_the_assignments_carries_no_role(
        self, sync, realm, environment, tenant_id, user_id, role_id, permission_id
    ):
        """The groups map no role and nothing maps the client roles: the token says who, never what."""
        role_name = f"sync-{role_id.hex[:8]}"
        await push_role(
            sync, role_id, permission_id, role_name=role_name, permission_name=f"t{permission_id.hex[:8]}:r"
        )
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        created = {
            "email": email_of(user_id),
            "status": "active",
            "given_name": "Token",
            "family_name": "Holder",
            CREATED_IN_TENANT: str(tenant_id),
        }
        await sync.sync_user(user_id=user_id, data=created)
        assert await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=role_id) == Ok(None)
        account = find_user(realm, user_id)
        # A local password for this test's own user, so it can log in through the SPA.
        password = f"local-only-{uuid.uuid4().hex}"
        realm.put(f"/users/{account['id']}", json={"emailVerified": True}).raise_for_status()
        realm.put(
            f"/users/{account['id']}/reset-password", json={"type": "password", "value": password, "temporary": False}
        ).raise_for_status()

        tokens = environment.login(
            KeycloakUser(str(user_id), password, account["id"]), client_id=SPA_CLIENT_ID, scope="openid organization"
        )

        _header, claims = decode_unverified(tokens.access_token)
        assert {key: claims.get(key) for key in ("sub", "aud", "organization", "resource_access", "realm_access")} == {
            "sub": account["id"],
            "aud": API_AUDIENCE,
            "organization": [str(tenant_id)],
            "resource_access": None,
            "realm_access": None,
        }
        mappings = realm.get(f"/users/{account['id']}/role-mappings")
        mappings.raise_for_status()
        assert mappings.json().get("clientMappings", {}).get(API_AUDIENCE) is None


@pytest.fixture
def linked_account(realm: httpx.Client) -> Iterator[dict]:
    """A Keycloak account the adapter did not create, as a user who signed in through Keycloak holds one."""
    username = f"signed-up-{uuid.uuid4().hex[:12]}"
    created = realm.post("/users", json={"username": username, "email": f"{username}@sync.test", "enabled": True})
    created.raise_for_status()
    account_id = created.headers["location"].rsplit("/", 1)[1]
    yield realm.get(f"/users/{account_id}").json()
    realm.delete(f"/users/{account_id}")


class TestLinkedAccounts:
    async def test_memberships_and_status_reach_the_account_the_link_names(
        self, environment, realm, tenant_id, user_id, role_id, permission_id, linked_account
    ):
        links = MemoryKeycloakLinks(subjects={user_id: linked_account["id"]})
        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
            sync = KeycloakSyncAdapter(await connect_keycloak_admin(http), links)
            await push_role(
                sync,
                role_id,
                permission_id,
                role_name=f"sync-{role_id.hex[:8]}",
                permission_name=f"l{permission_id.hex[:8]}:r",
            )
            await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
            organization = find_organization(realm, tenant_id)

            added = await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=role_id)
            assert (added, members(realm, organization)) == (Ok(None), [linked_account["username"]])

            disabled = await sync.sync_user(
                user_id=user_id, data={"email": email_of(user_id), "status": "inactive", STATUS_CHANGED: True}
            )
            removed = await sync.delete_role_assignment(
                user_id=user_id, tenant_id=tenant_id, role_id=role_id, data={LAST_ROLE_IN_TENANT: True}
            )

        account = realm.get(f"/users/{linked_account['id']}").json()
        assert (disabled, removed) == (Ok(None), Ok(None))
        assert members(realm, organization) == []
        assert {key: account[key] for key in ("enabled", "email")} == {
            "enabled": False,
            "email": linked_account["email"],
        }
        assert find_user(realm, user_id) is None


class TestSeededLinks:
    """A user ``seed_keycloak --link-users`` linked to an existing account of the realm, over Postgres."""

    async def test_a_profile_and_status_push_reaches_the_linked_account_and_creates_none(
        self, db_session, environment, realm, user_id, linked_account
    ):
        provider = (
            await db_session.execute(select(Provider).where(Provider.name == PROVIDER_NAME))
        ).scalar_one_or_none()
        if provider is None:
            provider = Provider(name=PROVIDER_NAME, type=ProviderType.oidc, issuer_url=environment.issuer)
            db_session.add(provider)
        account = SeededAccount(
            username=linked_account["username"],
            external_sub=linked_account["id"],
            email=linked_account["email"],
            given_name="Seeded",
            family_name="User",
        )
        user = linked_user(account, None, None)
        user.id = user_id
        db_session.add(user)
        await db_session.flush()
        db_session.add(
            IdPLink(
                user_id=user_id,
                provider_id=provider.id,
                external_sub=account.external_sub,
                external_email=account.email,
                metadata_={LINK_ORIGIN: LINKED_BY_BACKEND},
            )
        )
        await db_session.flush()

        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
            sync = KeycloakSyncAdapter(await connect_keycloak_admin(http), RepositoryKeycloakLinks(db_session))
            profile = await sync.sync_user(
                user_id=user_id,
                data={"email": account.email, "status": "active", "given_name": "Renamed", "family_name": "User"},
            )
            status = await sync.sync_user(
                user_id=user_id, data={"email": account.email, "status": "inactive", STATUS_CHANGED: True}
            )

        pushed = realm.get(f"/users/{account.external_sub}").json()
        assert (profile, status) == (Ok(None), Ok(None))
        # The status reached the seeded account; its email and names stay its own; no account under the id.
        assert {key: pushed.get(key) for key in ("enabled", "email", "firstName")} == {
            "enabled": False,
            "email": account.email,
            "firstName": None,
        }
        assert find_user(realm, user_id) is None


class TestCanonicalLinks:
    """The adapter over Postgres: what it creates is linked so the first sign-in finds it."""

    async def test_a_new_tenant_and_a_user_created_in_it_are_linked(
        self, db_session, environment, realm, tenant_id, user_id
    ):
        provider = (
            await db_session.execute(select(Provider).where(Provider.name == PROVIDER_NAME))
        ).scalar_one_or_none()
        if provider is None:
            provider = Provider(name=PROVIDER_NAME, type=ProviderType.oidc, issuer_url=environment.issuer)
            db_session.add(provider)
        db_session.add(Tenant(id=tenant_id, name=f"Sync {tenant_id}"))
        db_session.add(User(id=user_id, email=email_of(user_id), user_name=str(user_id)))
        await db_session.flush()

        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
            sync = KeycloakSyncAdapter(await connect_keycloak_admin(http), RepositoryKeycloakLinks(db_session))
            tenant_pushed = await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
            user_pushed = await sync.sync_user(
                user_id=user_id,
                data={"email": email_of(user_id), "status": "active", CREATED_IN_TENANT: str(tenant_id)},
            )

        tenant = await db_session.get(Tenant, tenant_id)
        links = (await db_session.execute(select(IdPLink).where(IdPLink.user_id == user_id))).scalars().all()
        assert (tenant_pushed, user_pushed) == (Ok(None), Ok(None))
        assert (tenant.external_org_id, tenant.external_org_provider_id) == (str(tenant_id), provider.id)
        assert [(link.provider_id, link.external_sub, link.metadata_) for link in links] == [
            (provider.id, find_user(realm, user_id)["id"], {LINK_ORIGIN: LINKED_BY_BACKEND})
        ]


class TestDeletingARole:
    """RoleService over Postgres and the compose Keycloak: a role delete ends the memberships it held alone."""

    async def test_deleting_a_users_only_role_ends_the_membership_and_another_role_keeps_one(
        self, db_session, sync, realm, tenant_id, user_id, other_user_id, permission_id
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
        permission_name = f"d{permission_id.hex[:8]}:r"
        assert await sync.sync_permission(permission_id=permission_id, data={"name": permission_name}) == Ok(None)
        for role in (deleted, kept):
            assert await sync.sync_role(role_id=role.id, data={"name": role.name, "tenant_id": None}) == Ok(None)
        await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})
        for uid in (user_id, other_user_id):
            await sync.sync_user(user_id=uid, data={"email": email_of(uid), "status": "active"})
            await sync.sync_role_assignment(user_id=uid, tenant_id=tenant_id, role_id=deleted.id)
        await sync.sync_role_assignment(user_id=other_user_id, tenant_id=tenant_id, role_id=kept.id)
        organization = find_organization(realm, tenant_id)
        service = RoleService(
            repository=RoleRepository(db_session),
            permission_repository=PermissionRepository(db_session),
            assignment_repository=UserTenantRoleRepository(db_session),
            adapter=sync,
            publisher=AsyncMock(),
        )
        try:
            result = await service.delete_role(role_id=deleted.id)

            assert result == Ok({"status": "deleted", "name": deleted.name})
            assert members(realm, organization) == [str(other_user_id)]
            assert {name: group["members"] for name, group in groups(realm, organization).items()} == {
                kept.name: [str(other_user_id)]
            }
            assert client_role(realm, ROLE_ATTRIBUTE, deleted.id) is None
        finally:
            _cleanup_client_roles(realm, ROLE_ATTRIBUTE, deleted.id)
            _cleanup_client_roles(realm, ROLE_ATTRIBUTE, kept.id)


class TestCredential:
    async def test_a_wrong_secret_is_refused(self, environment, monkeypatch):
        monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, "wrong")

        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
            with pytest.raises(KeycloakAdminError) as refused:
                await connect_keycloak_admin(http)

        assert refused.value.status == 401

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
