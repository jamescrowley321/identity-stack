"""KeycloakSyncAdapter against the compose Keycloak (``keycloak-test``, ``--profile keycloak``).

The adapter writes with the realm's admin client (``identity-stack-admin``, its
service account holding ``manage-users`` and ``manage-organizations``), through
the shipped ``connect_keycloak_admin``. What it wrote is read back with the
realm admin's own token. Every tenant and user is new to the test, named by a
fresh id, and removed afterwards whatever the outcome.
"""

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from expression import Ok
from fastapi import FastAPI

from app import main
from app.dependencies import identity
from app.services.adapters.keycloak import KeycloakSyncAdapter
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    ADMIN_TIMEOUT_SECONDS,
    ISSUER_VARIABLE,
    KeycloakAdminError,
    connect_keycloak_admin,
)
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
        assert await sync.sync_user(user_id=user_id, data={"email": renamed, "status": "inactive"}) == Ok(None)
        assert {key: find_user(realm, user_id)[key] for key in ("id", "email", "enabled")} == {
            "id": user["id"],
            "email": renamed,
            "enabled": False,
        }

        assert await sync.sync_user(user_id=user_id, data={"email": renamed, "status": "active"}) == Ok(None)
        assert find_user(realm, user_id)["enabled"] is True

        assert await sync.delete_user(user_id=user_id) == Ok(None)
        assert find_user(realm, user_id) is None
        assert await sync.delete_user(user_id=user_id) == Ok(None)

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

    async def test_adding_to_a_tenant_not_pushed_is_an_error(self, sync, tenant_id, user_id):
        await sync.sync_user(user_id=user_id, data={"email": email_of(user_id), "status": "active"})

        result = await sync.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())

        assert result.is_error()
        assert result.error.operation == "sync_role_assignment"


class TestCredential:
    async def test_a_wrong_secret_is_refused(self, environment, monkeypatch):
        monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, "wrong")

        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
            with pytest.raises(KeycloakAdminError) as refused:
                await connect_keycloak_admin(http)

        assert refused.value.status == 401

    async def test_the_read_only_directory_client_cannot_write(self, environment, monkeypatch, tenant_id):
        monkeypatch.setenv(ADMIN_CLIENT_ID_VARIABLE, DIRECTORY_CLIENT_ID)
        monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, DIRECTORY_CLIENT_SECRET)

        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
            sync = KeycloakSyncAdapter(await connect_keycloak_admin(http))
            result = await sync.sync_tenant(tenant_id=tenant_id, data={"name": f"Sync {tenant_id}"})

        assert result.is_error()
        assert result.error.context["status"] == 403

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
