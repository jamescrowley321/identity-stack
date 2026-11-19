"""The role and permission routes for a Keycloak admin, against ``keycloak-test`` and the test Postgres.

A deployment that syncs to Keycloak (``SYNC_PROVIDER=keycloak``) and authorizes
Keycloak principals from the canonical model. Tokens are the realm's own, through
the shipped middleware on the shipped Keycloak config. The routes, services and
``KeycloakSyncAdapter`` are the shipped wiring (``get_role_service``,
``get_permission_service``) over the shipped admin client: nothing is stubbed but
the cache publisher. Each change is read back from the realm with the realm
admin's own token. Every client role, group and account the test makes is named
by a fresh id and removed afterwards whatever the outcome.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.dependencies import identity, rbac
from app.dependencies.rbac import get_rbac_session
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import keycloak_config
from app.middleware.rate_limit import limiter
from app.models.database import get_async_session
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import LINK_ORIGIN, LINKED_BY_BACKEND, IdPLink, User, UserStatus
from app.routers import permissions, roles
from app.services.adapters.keycloak import PROVIDER_NAME
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    ADMIN_TIMEOUT_SECONDS,
    API_CLIENT_VARIABLE,
    ISSUER_VARIABLE,
    connect_keycloak_admin,
)
from tests.helpers.identity_fixture import IDENTITIES
from tests.helpers.keycloak import ACME_SCOPE, BOB, DAVE, KeycloakTokenSource
from tests.helpers.keycloak_model import ADMIN_CLIENT_ID, ADMIN_CLIENT_SECRET, API_AUDIENCE, SPA_CLIENT_ID
from tests.integration.test_keycloak_sync_adapter import client_roles, composites, find_organization, groups, members

NO_TENANT = {"detail": "No tenant context"}


@pytest.fixture
def keycloak_deployment(monkeypatch, keycloak_token_source: KeycloakTokenSource) -> KeycloakTokenSource:
    monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "keycloak"}))
    monkeypatch.setenv(ISSUER_VARIABLE, keycloak_token_source.issuer)
    monkeypatch.setenv(ADMIN_CLIENT_ID_VARIABLE, ADMIN_CLIENT_ID)
    monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, ADMIN_CLIENT_SECRET)
    monkeypatch.setenv(API_CLIENT_VARIABLE, API_AUDIENCE)
    return keycloak_token_source


@pytest.fixture
def realm(keycloak_token_source: KeycloakTokenSource) -> Iterator[httpx.Client]:
    """The realm admin's view of the realm, to read back what the routes pushed."""
    with keycloak_token_source._admin() as admin:
        yield admin


@pytest_asyncio.fixture(loop_scope="session")
async def client(keycloak_deployment, db_session) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.state.limiter = limiter
    app.state.cache_publisher = AsyncMock()
    app.include_router(roles.router, prefix="/api")
    app.include_router(permissions.router, prefix="/api")

    async def _session():
        yield db_session

    app.dependency_overrides[get_rbac_session] = _session
    app.dependency_overrides[get_async_session] = _session
    keycloak = keycloak_config(keycloak_deployment.issuer, API_AUDIENCE, SPA_CLIENT_ID, require_https=False)
    app.add_middleware(TokenValidationMiddleware, providers=[keycloak])
    limiter.reset()
    async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
        app.state.keycloak_admin = await connect_keycloak_admin(http)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            yield api


async def _provider(db_session, issuer: str) -> Provider:
    provider = (await db_session.execute(select(Provider).where(Provider.name == PROVIDER_NAME))).scalar_one_or_none()
    if provider is None:
        provider = Provider(name=PROVIDER_NAME, type=ProviderType.oidc, issuer_url=issuer)
        db_session.add(provider)
        await db_session.flush()
    return provider


async def _tenant(db_session, provider: Provider, key: str) -> Tenant:
    """A canonical tenant linked to the realm's organization ``key``, as ``make keycloak-seed`` links it."""
    tenant = Tenant(
        name=f"{key}-{uuid.uuid4()}",
        external_org_id=key,
        external_org_provider_id=provider.id,
        status=TenantStatus.active,
        domains=[IDENTITIES.tenants[key].domain],
    )
    db_session.add(tenant)
    await db_session.flush()
    return tenant


async def _global_role(db_session, name: str) -> Role:
    role = (await db_session.execute(select(Role).where(Role.name == name, Role.tenant_id.is_(None)))).scalars().first()
    if role is None:
        role = Role(name=name)
        db_session.add(role)
        await db_session.flush()
    return role


async def _linked_user(db_session, provider: Provider, sub: str, email: str, username: str) -> User:
    user = User(email=email, user_name=username, status=UserStatus.active)
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        IdPLink(
            user_id=user.id,
            provider_id=provider.id,
            external_sub=sub,
            external_email=email,
            metadata_={LINK_ORIGIN: LINKED_BY_BACKEND},
        )
    )
    await db_session.flush()
    return user


async def _assign(db_session, user: User, tenant: Tenant, role_name: str) -> None:
    role = await _global_role(db_session, role_name)
    db_session.add(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
    await db_session.flush()


@pytest.fixture
def account(realm: httpx.Client) -> Iterator[dict]:
    """A realm account for the member the admin assigns a role to, removed afterwards."""
    username = f"assigned-{uuid.uuid4().hex[:12]}"
    created = realm.post("/users", json={"username": username, "email": f"{username}@acme.test", "enabled": True})
    created.raise_for_status()
    account_id = created.headers["location"].rsplit("/", 1)[1]
    yield realm.get(f"/users/{account_id}").json()
    realm.delete(f"/users/{account_id}")


def _client_role(realm: httpx.Client, name: str) -> dict | None:
    return next((role for role in client_roles(realm) if role["name"] == name), None)


def _remove_client_roles(realm: httpx.Client, names: list[str]) -> None:
    for name in names:
        role = _client_role(realm, name)
        if role is not None:
            realm.delete(f"/roles-by-id/{role['id']}").raise_for_status()


def _remove_groups(realm: httpx.Client, organization: dict, names: list[str]) -> None:
    response = realm.get(f"/organizations/{organization['id']}/groups")
    response.raise_for_status()
    for group in response.json():
        if group["name"] in names:
            realm.delete(f"/organizations/{organization['id']}/groups/{group['id']}")


def _bearer(token: str, tenant: Tenant) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", TENANT_SELECTOR_HEADER: str(tenant.id)}


class TestAKeycloakAdmin:
    async def test_manages_roles_and_permissions_and_each_change_reaches_keycloak(
        self, keycloak_deployment, client, realm, db_session, account
    ):
        provider = await _provider(db_session, keycloak_deployment.issuer)
        acme = await _tenant(db_session, provider, "acme")
        dave = await _linked_user(db_session, provider, DAVE.id, IDENTITIES.users[DAVE.username].email, DAVE.username)
        await _assign(db_session, dave, acme, "admin")
        # The member already belongs to acme: a role is assigned to a member of the tenant.
        member = await _linked_user(db_session, provider, account["id"], account["email"], account["username"])
        await _assign(db_session, member, acme, "member")
        headers = _bearer(keycloak_deployment.login(DAVE, client_id=SPA_CLIENT_ID, scope=ACME_SCOPE).access_token, acme)
        suffix = uuid.uuid4().hex[:8]
        permission, renamed_permission, role = f"rr{suffix}:read", f"rr{suffix}:write", f"rr-{suffix}"
        organization = find_organization(realm, "acme")
        try:
            created_permission = await client.post("/api/permissions", json={"name": permission}, headers=headers)
            assert created_permission.status_code == 201, created_permission.text
            assert _client_role(realm, permission) is not None

            created_role = await client.post(
                "/api/roles", json={"name": role, "permission_names": [permission]}, headers=headers
            )
            assert created_role.status_code == 201, created_role.text
            assert composites(realm, _client_role(realm, role)) == [permission]

            listed_roles = await client.get("/api/roles", headers=headers)
            listed_permissions = await client.get("/api/permissions", headers=headers)
            assert role in {entry["name"] for entry in listed_roles.json()["roles"]}
            assert permission in {entry["name"] for entry in listed_permissions.json()["permissions"]}

            updated_permission = await client.put(
                f"/api/permissions/{permission}", json={"new_name": renamed_permission}, headers=headers
            )
            assert updated_permission.status_code == 200, updated_permission.text
            assert (_client_role(realm, permission), composites(realm, _client_role(realm, role))) == (
                None,
                [renamed_permission],
            )

            updated_role = await client.put(f"/api/roles/{role}", json={"description": "Changed"}, headers=headers)
            assert updated_role.status_code == 200, updated_role.text
            assert _client_role(realm, role)["description"] == "Changed"

            assignment = {"user_id": str(member.id), "tenant_id": str(acme.id), "role_names": [role]}
            assigned = await client.post("/api/roles/assign", json=assignment, headers=headers)
            assert assigned.status_code == 200, assigned.text
            assert account["username"] in members(realm, organization)
            assert groups(realm, organization)[role]["members"] == [account["username"]]

            removed = await client.post("/api/roles/remove", json=assignment, headers=headers)
            assert removed.status_code == 200, removed.text
            # The member keeps the tenant through its other role: only the role's group is left.
            assert account["username"] in members(realm, organization)
            assert groups(realm, organization)[role]["members"] == []

            deleted_role = await client.delete(f"/api/roles/{role}", headers=headers)
            assert deleted_role.status_code == 200, deleted_role.text
            assert (_client_role(realm, role), role in groups(realm, organization)) == (None, False)

            deleted_permission = await client.delete(f"/api/permissions/{renamed_permission}", headers=headers)
            assert deleted_permission.status_code == 200, deleted_permission.text
            assert _client_role(realm, renamed_permission) is None
        finally:
            _remove_groups(realm, organization, [role])
            _remove_client_roles(realm, [role, permission, renamed_permission])

    async def test_of_one_of_two_tenants_cannot_change_the_definitions(
        self, keycloak_deployment, client, realm, db_session
    ):
        """``bob`` administers acme and views globex: the definitions are every tenant's, so he is refused them."""
        provider = await _provider(db_session, keycloak_deployment.issuer)
        acme = await _tenant(db_session, provider, "acme")
        globex = await _tenant(db_session, provider, "globex")
        bob = await _linked_user(db_session, provider, BOB.id, IDENTITIES.users[BOB.username].email, BOB.username)
        await _assign(db_session, bob, acme, "admin")
        await _assign(db_session, bob, globex, "viewer")
        headers = _bearer(keycloak_deployment.login(BOB, client_id=SPA_CLIENT_ID, scope=ACME_SCOPE).access_token, acme)
        name = f"rr-{uuid.uuid4().hex[:8]}"
        try:
            created = await client.post("/api/roles", json={"name": name}, headers=headers)
            listed = await client.get("/api/permissions", headers=headers)
        finally:
            _remove_client_roles(realm, [name])

        assert {"create": (created.status_code, created.json()), "list": (listed.status_code, listed.json())} == {
            "create": (403, NO_TENANT),
            "list": (403, NO_TENANT),
        }
        assert _client_role(realm, name) is None
