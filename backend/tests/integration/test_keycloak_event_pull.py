"""The Keycloak event pull against the compose Keycloak (``keycloak-test``) and the test Postgres.

Each test changes the realm as an administrator does in the console (the
``master`` realm's admin, through the Admin API the console calls), runs one
poll of the shipped ``KeycloakPullService`` on the realm's admin client, and reads
Postgres. Every test starts the pull afresh (``keycloak_pull_state`` is rolled
back with the test's transaction), so its first poll places the cursors at the
newest events and the changes made afterwards are the ones applied. Every
organization, user and client role the test makes is new, named by a fresh id,
and removed afterwards whatever the outcome.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from expression import Ok
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.dependencies import rbac
from app.dependencies.identity import get_identity_resolution_service
from app.dependencies.keycloak_pull import build_keycloak_pull_service
from app.dependencies.keycloak_sync import RepositoryKeycloakLinks
from app.dependencies.rbac import get_rbac_session
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import descope_config, keycloak_config
from app.middleware.rate_limit import limiter
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Permission, Role, RolePermission
from app.models.identity.sync_event import SyncEvent, SyncEventVerb
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import LINK_ORIGIN, LINKED_AT_SIGN_IN, IdPLink, User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.routers import access_checks
from app.services.adapters.base import STATUS_CHANGED
from app.services.adapters.keycloak import KeycloakSyncAdapter
from app.services.identity_resolution import IdentityResolutionService
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    ADMIN_TIMEOUT_SECONDS,
    API_CLIENT_VARIABLE,
    ISSUER_VARIABLE,
    KeycloakAdminApi,
    connect_keycloak_admin,
)
from tests.helpers.keycloak import CAROL, ERIN, KeycloakTokenSource
from tests.helpers.keycloak_model import ADMIN_CLIENT_ID, ADMIN_CLIENT_SECRET, API_AUDIENCE, SPA_CLIENT_ID
from tests.helpers.keycloak_target import KeycloakUser

KEYCLOAK = "keycloak"
PAGE_SIZE = 100
ADMIN_ROLE = "/api/access-checks/admin-role"
NO_TENANT = {"detail": "No tenant context"}


@pytest.fixture
def environment(monkeypatch, keycloak_token_source: KeycloakTokenSource) -> KeycloakTokenSource:
    monkeypatch.setenv(ISSUER_VARIABLE, keycloak_token_source.issuer)
    monkeypatch.setenv(ADMIN_CLIENT_ID_VARIABLE, ADMIN_CLIENT_ID)
    monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, ADMIN_CLIENT_SECRET)
    monkeypatch.setenv(API_CLIENT_VARIABLE, API_AUDIENCE)
    return keycloak_token_source


@pytest_asyncio.fixture(loop_scope="session")
async def api(environment) -> AsyncIterator[KeycloakAdminApi]:
    async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
        yield await connect_keycloak_admin(http)


@pytest.fixture
def console(keycloak_token_source: KeycloakTokenSource) -> Iterator[httpx.Client]:
    """The realm as an administrator changes it in the console: the ``master`` realm's admin."""
    with keycloak_token_source._admin() as admin:
        yield admin


@dataclass
class Pull:
    """Polls as a running backend does: a service built afresh for each poll, on the test's session."""

    api: KeycloakAdminApi
    session: object

    async def poll(self) -> dict:
        result = await build_keycloak_pull_service(self.api, self.session, page_size=PAGE_SIZE).poll()
        assert result.is_ok(), result
        return result.ok


@pytest_asyncio.fixture(loop_scope="session")
async def provider(db_session) -> Provider:
    provider = Provider(name=KEYCLOAK, type=ProviderType.oidc, issuer_url="unused-by-these-tests")
    db_session.add(provider)
    await db_session.flush()
    return provider


@pytest_asyncio.fixture(loop_scope="session")
async def pull(api, db_session, provider) -> Pull:
    """The pull, started: its first poll has placed the cursors at the newest events."""
    started = Pull(api, db_session)
    assert (await started.poll()).get("started") == 1
    return started


# --- realm objects the tests make and remove ---------------------------------------------


@pytest.fixture
def organization(console: httpx.Client) -> Iterator[dict]:
    alias = str(uuid.uuid4())
    console.post(
        "/organizations",
        json={"name": f"Pull {alias}", "alias": alias, "domains": [{"name": f"{alias[:8]}.pull.test"}]},
    ).raise_for_status()
    found = [entry for entry in console.get("/organizations", params={"q": f"alias:{alias}"}).json()]
    yield found[0]
    console.delete(f"/organizations/{found[0]['id']}")


@pytest.fixture
def account(console: httpx.Client) -> Iterator[dict]:
    username = f"pull-{uuid.uuid4()}"
    console.post(
        "/users",
        json={"username": username, "email": f"{username}@pull.test", "enabled": True, "firstName": "Pull"},
    ).raise_for_status()
    found = console.get("/users", params={"username": username, "exact": "true"}).json()
    yield found[0]
    console.delete(f"/users/{found[0]['id']}")


@pytest.fixture
def client_role_names(console: httpx.Client, api_client_id: str) -> Iterator[list[str]]:
    """Names of API client roles a test makes; each is deleted afterwards, whatever its name became."""
    names: list[str] = []
    yield names
    for role in console.get(f"/clients/{api_client_id}/roles").json():
        if role["name"] in names:
            console.delete(f"/roles-by-id/{role['id']}")


@pytest.fixture
def api_client_id(console: httpx.Client) -> str:
    return console.get("/clients", params={"clientId": API_AUDIENCE}).json()[0]["id"]


# --- canonical rows -----------------------------------------------------------------------


async def _tenant(db_session, provider: Provider, organization: dict) -> Tenant:
    tenant = Tenant(
        name=organization["name"],
        external_org_id=organization["alias"],
        external_org_provider_id=provider.id,
        domains=[entry["name"] for entry in organization["domains"]],
        status=TenantStatus.active,
    )
    db_session.add(tenant)
    await db_session.flush()
    return tenant


async def _user(db_session, provider: Provider, keycloak_id: str, email: str) -> User:
    user = User(email=email, user_name=email, given_name="Pull", status=UserStatus.active)
    db_session.add(user)
    await db_session.flush()
    # Linked as a first sign-in links an account: the adapter follows such a link.
    db_session.add(
        IdPLink(
            user_id=user.id,
            provider_id=provider.id,
            external_sub=keycloak_id,
            external_email=email,
            metadata_={LINK_ORIGIN: LINKED_AT_SIGN_IN},
        )
    )
    await db_session.flush()
    return user


async def _global_role(db_session, name: str) -> Role:
    role = (await db_session.execute(select(Role).where(Role.name == name, Role.tenant_id.is_(None)))).scalars().first()
    if role is None:
        role = Role(name=name)
        db_session.add(role)
        await db_session.flush()
    return role


async def _roles_of(db_session, user: User, tenant: Tenant) -> list[str]:
    rows = await db_session.execute(
        select(Role.name)
        .join(UserTenantRole, UserTenantRole.role_id == Role.id)
        .where(UserTenantRole.user_id == user.id, UserTenantRole.tenant_id == tenant.id)
    )
    return sorted(rows.scalars().all())


async def _reload(db_session, row):
    await db_session.refresh(row)
    return row


async def _sync_events(db_session, provider: Provider) -> list[tuple[SyncEventVerb, str, str]]:
    # A poll with nothing to apply rolls its transaction back, which expires what the session holds.
    await db_session.refresh(provider)
    rows = await db_session.execute(
        select(SyncEvent.verb, SyncEvent.subject_type, SyncEvent.subject_id)
        .where(SyncEvent.provider_id == provider.id)
        .order_by(SyncEvent.occurred_at, SyncEvent.subject_type)
    )
    return [tuple(row) for row in rows.all()]


# --- each change of the "In" column reaches Postgres within one poll ----------------------


class TestTenants:
    async def test_an_organization_update_disable_and_delete(self, console, organization, db_session, provider, pull):
        tenant = await _tenant(db_session, provider, organization)
        path = f"/organizations/{organization['id']}"
        current = console.get(path).json()
        renamed = {**current, "name": f"Renamed {organization['alias']}", "enabled": False}
        renamed["domains"] = [{"name": f"{organization['alias'][:8]}.renamed.test"}]
        console.put(path, json=renamed).raise_for_status()

        assert (await pull.poll())["applied"] == 1
        tenant = await _reload(db_session, tenant)
        assert (tenant.name, tenant.domains, tenant.status) == (
            renamed["name"],
            [renamed["domains"][0]["name"]],
            TenantStatus.suspended,
        )

        console.put(path, json={**console.get(path).json(), "enabled": True}).raise_for_status()
        await pull.poll()
        assert (await _reload(db_session, tenant)).status == TenantStatus.active

        console.delete(path).raise_for_status()
        await pull.poll()
        assert (await _reload(db_session, tenant)).status == TenantStatus.suspended
        assert (SyncEventVerb.deleted, "tenant", str(tenant.id)) in await _sync_events(db_session, provider)


class TestUsers:
    async def test_an_account_update_disable_and_delete(self, console, account, db_session, provider, pull):
        user = await _user(db_session, provider, account["id"], account["email"])
        path = f"/users/{account['id']}"
        changed_email = f"changed-{account['email']}"
        console.put(
            path, json={**console.get(path).json(), "firstName": "Given", "lastName": "Family", "email": changed_email}
        ).raise_for_status()

        await pull.poll()
        user = await _reload(db_session, user)
        assert (user.given_name, user.family_name, user.email, user.status) == (
            "Given",
            "Family",
            changed_email,
            UserStatus.active,
        )

        console.put(path, json={**console.get(path).json(), "enabled": False}).raise_for_status()
        await pull.poll()
        assert (await _reload(db_session, user)).status == UserStatus.inactive

        console.put(path, json={**console.get(path).json(), "enabled": True}).raise_for_status()
        await pull.poll()
        assert (await _reload(db_session, user)).status == UserStatus.active

        console.delete(path).raise_for_status()
        await pull.poll()
        assert (await _reload(db_session, user)).status == UserStatus.inactive
        assert (SyncEventVerb.deleted, "user", str(user.id)) in await _sync_events(db_session, provider)


class TestMembershipsAndAssignments:
    async def test_member_and_role_group_added_and_removed(
        self, console, organization, account, db_session, provider, pull
    ):
        tenant = await _tenant(db_session, provider, organization)
        user = await _user(db_session, provider, account["id"], account["email"])
        await _global_role(db_session, "member")
        await _global_role(db_session, "admin")
        base = f"/organizations/{organization['id']}"

        console.post(f"{base}/members", json=account["id"]).raise_for_status()
        await pull.poll()
        assert await _roles_of(db_session, user, tenant) == ["member"]

        console.post(f"{base}/groups", json={"name": "admin"}).raise_for_status()
        group = [entry for entry in console.get(f"{base}/groups").json() if entry["name"] == "admin"][0]
        console.put(f"{base}/groups/{group['id']}/members/{account['id']}").raise_for_status()
        await pull.poll()
        assert await _roles_of(db_session, user, tenant) == ["admin", "member"]

        console.delete(f"{base}/groups/{group['id']}/members/{account['id']}").raise_for_status()
        await pull.poll()
        assert await _roles_of(db_session, user, tenant) == ["member"]

        console.put(f"{base}/groups/{group['id']}/members/{account['id']}").raise_for_status()
        console.delete(f"{base}/members/{account['id']}").raise_for_status()
        await pull.poll()
        assert await _roles_of(db_session, user, tenant) == []


class TestPermissionsAndRoles:
    async def test_client_roles_created_updated_composed_and_deleted(
        self, console, api_client_id, client_role_names, db_session, pull
    ):
        token = uuid.uuid4().hex[:8]
        permission, renamed, role = f"pull{token}:read", f"pull{token}:write", f"pull-{token}"
        client_role_names.extend([permission, renamed, role])
        roles = f"/clients/{api_client_id}/roles"
        console.post(roles, json={"name": permission, "description": "Read"}).raise_for_status()
        console.post(roles, json={"name": role, "description": "A role"}).raise_for_status()
        permission_kc = console.get(f"{roles}/{permission}").json()
        role_kc = console.get(f"{roles}/{role}").json()

        await pull.poll()
        held = await db_session.execute(
            select(Permission.name, Permission.description).where(Permission.name == permission)
        )
        assert [tuple(row) for row in held.all()] == [(permission, "Read")]
        canonical_role = (await db_session.execute(select(Role).where(Role.name == role))).scalars().one()
        assert (canonical_role.tenant_id, canonical_role.description) == (None, "A role")

        console.post(f"/roles-by-id/{role_kc['id']}/composites", json=[{"id": permission_kc["id"]}]).raise_for_status()
        await pull.poll()
        mapped = await db_session.execute(
            select(Permission.name)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == canonical_role.id)
        )
        assert mapped.scalars().all() == [permission]

        console.put(
            f"/roles-by-id/{permission_kc['id']}", json={"name": renamed, "description": "Write"}
        ).raise_for_status()
        await pull.poll()
        names = await db_session.execute(select(Permission.name).where(Permission.name.in_([permission, renamed])))
        assert names.scalars().all() == [renamed]

        console.request(
            "DELETE", f"/roles-by-id/{role_kc['id']}/composites", json=[{"id": permission_kc["id"]}]
        ).raise_for_status()
        await pull.poll()
        count = select(func.count()).select_from(RolePermission).where(RolePermission.role_id == canonical_role.id)
        assert (await db_session.execute(count)).scalar() == 0

        console.delete(f"/roles-by-id/{permission_kc['id']}").raise_for_status()
        console.delete(f"/roles-by-id/{role_kc['id']}").raise_for_status()
        await pull.poll()
        gone = await db_session.execute(
            select(func.count()).select_from(Permission).where(Permission.name.in_([permission, renamed]))
        )
        assert gone.scalar() == 0
        assert (await db_session.execute(select(Role).where(Role.name == role))).scalars().first() is None


# --- revocation: refused on the next request after the poll -------------------------------


@pytest.fixture
def app_client(db_session, keycloak_token_source, monkeypatch) -> AsyncClient:
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", KEYCLOAK}))
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(access_checks.router, prefix="/api")

    async def _session():
        yield db_session

    async def _resolver() -> IdentityResolutionService:
        return IdentityResolutionService(
            user_repository=UserRepository(db_session),
            idp_link_repository=IdPLinkRepository(db_session),
            provider_repository=ProviderRepository(db_session),
            assignment_repository=UserTenantRoleRepository(db_session),
            role_repository=RoleRepository(db_session),
            tenant_repository=TenantRepository(db_session),
            redis_client=None,
        )

    app.dependency_overrides[get_identity_resolution_service] = _resolver
    app.dependency_overrides[get_rbac_session] = _session
    keycloak = keycloak_config(keycloak_token_source.issuer, API_AUDIENCE, SPA_CLIENT_ID, require_https=False)
    app.add_middleware(TokenValidationMiddleware, providers=[descope_config(""), keycloak])
    limiter.reset()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _bearer(token: str, tenant: Tenant) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", TENANT_SELECTOR_HEADER: str(tenant.id)}


async def _tenant_admin(db_session, provider, organization, fixture_user: KeycloakUser) -> tuple[Tenant, User]:
    tenant = await _tenant(db_session, provider, organization)
    user = await _user(db_session, provider, fixture_user.id, f"{fixture_user.username}@pull.test")
    db_session.add(
        UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=(await _global_role(db_session, "admin")).id)
    )
    await db_session.flush()
    return tenant, user


class TestRevocation:
    async def test_a_user_disabled_in_keycloak_is_refused_after_the_poll(
        self, console, organization, db_session, provider, pull, app_client, keycloak_token_source
    ):
        tenant, _user_row = await _tenant_admin(db_session, provider, organization, ERIN)
        token = keycloak_token_source.login(ERIN, client_id=SPA_CLIENT_ID, scope="openid").access_token
        assert (await app_client.get(ADMIN_ROLE, headers=_bearer(token, tenant))).status_code == 200

        path = f"/users/{ERIN.id}"
        console.put(path, json={**console.get(path).json(), "enabled": False}).raise_for_status()
        try:
            await pull.poll()
            response = await app_client.get(ADMIN_ROLE, headers=_bearer(token, tenant))
        finally:
            console.put(path, json={**console.get(path).json(), "enabled": True}).raise_for_status()

        assert (response.status_code, response.json()) == (403, NO_TENANT)

    async def test_a_user_removed_from_the_organization_is_refused_after_the_poll(
        self, console, organization, db_session, provider, pull, app_client, keycloak_token_source
    ):
        tenant, user = await _tenant_admin(db_session, provider, organization, CAROL)
        members = f"/organizations/{organization['id']}/members"
        console.post(members, json=CAROL.id).raise_for_status()
        try:
            token = keycloak_token_source.login(CAROL, client_id=SPA_CLIENT_ID, scope="openid").access_token
            await pull.poll()
            assert (await app_client.get(ADMIN_ROLE, headers=_bearer(token, tenant))).status_code == 200

            console.delete(f"{members}/{CAROL.id}").raise_for_status()
            await pull.poll()
            response = await app_client.get(ADMIN_ROLE, headers=_bearer(token, tenant))
        finally:
            console.delete(f"{members}/{CAROL.id}")

        assert (response.status_code, response.json()) == (403, NO_TENANT)
        assert await _roles_of(db_session, user, tenant) == []


# --- the backend's own pushes, and a restart -------------------------------------------------


class TestOwnPushes:
    async def test_produce_no_inbound_change(self, api, organization, account, db_session, provider, pull):
        tenant = await _tenant(db_session, provider, organization)
        user = await _user(db_session, provider, account["id"], account["email"])
        await _global_role(db_session, "member")
        adapter = KeycloakSyncAdapter(api, RepositoryKeycloakLinks(db_session))
        before = await _sync_events(db_session, provider)

        # Pushes that do not match the canonical rows: were they pulled back, the rows would change.
        assert await adapter.sync_tenant(
            tenant_id=tenant.id, data={"name": f"Pushed {tenant.id}", "status": "suspended"}
        ) == Ok(None)
        assert await adapter.sync_user(
            user_id=user.id, data={"email": account["email"], "status": "inactive", STATUS_CHANGED: True}
        ) == Ok(None)
        stats = await pull.poll()

        assert stats["own"] >= 1
        assert (stats["applied"], stats["failed"]) == (0, 0)
        tenant, user = await _reload(db_session, tenant), await _reload(db_session, user)
        assert (tenant.name, tenant.status, user.status) == (
            organization["name"],
            TenantStatus.active,
            UserStatus.active,
        )
        assert await _sync_events(db_session, provider) == before


class TestRestart:
    async def test_resumes_from_the_cursor_without_reapplying(
        self, api, console, organization, db_session, provider, pull
    ):
        tenant = await _tenant(db_session, provider, organization)
        path = f"/organizations/{organization['id']}"
        console.put(
            path, json={**console.get(path).json(), "name": f"First {organization['alias']}"}
        ).raise_for_status()
        assert (await pull.poll())["applied"] == 1
        recorded = await _sync_events(db_session, provider)

        # Canonical changes since: an event applied again would undo them.
        tenant = await _reload(db_session, tenant)
        tenant.name = f"Canonical {organization['alias']}"
        await db_session.commit()

        restarted = Pull(api, db_session)
        stats = await restarted.poll()

        assert (stats["applied"], stats["skipped"], stats["failed"]) == (0, 0, 0)
        assert (await _reload(db_session, tenant)).name == f"Canonical {organization['alias']}"
        assert await _sync_events(db_session, provider) == recorded

        console.put(
            path, json={**console.get(path).json(), "name": f"Second {organization['alias']}"}
        ).raise_for_status()
        assert (await restarted.poll())["applied"] == 1
        assert (await _reload(db_session, tenant)).name == f"Second {organization['alias']}"
