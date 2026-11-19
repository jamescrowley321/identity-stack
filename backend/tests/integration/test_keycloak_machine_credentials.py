"""Machine credentials on Keycloak, against ``keycloak-test`` and the test Postgres (#680).

``/api/keys`` is the ``machine_credentials`` capability. With ``SYNC_PROVIDER``
``keycloak``, a credential is a canonical row and a confidential client the sync
adapter registers in the realm, with a service account and no browser flow, owned
by the tenant's organization. Everything below goes through the shipped router,
middleware, dependencies and adapter: the client is registered in the container,
its secret is exchanged there for a client-credentials token, and that token is
sent back to the API. It is accepted in its own tenant only, and refused once the
credential is deactivated or deleted, before the token expires.
"""

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.dependencies import identity, rbac
from app.dependencies.rbac import get_rbac_session
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.errors.identity import IdentityErrorRaised
from app.errors.problem_detail import identity_error_raised_handler
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import KEYCLOAK_MACHINE_CLIENT_PREFIX, descope_config, keycloak_config
from app.middleware.rate_limit import limiter
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import IdPLink, User, UserStatus
from app.routers import access_checks, accesskeys
from app.services.adapters.keycloak import CREDENTIAL_ATTRIBUTE, ORGANIZATION_ATTRIBUTE, TENANT_ATTRIBUTE
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    ADMIN_TIMEOUT_SECONDS,
    API_CLIENT_VARIABLE,
    ISSUER_VARIABLE,
    KeycloakAdminApi,
    connect_keycloak_admin,
)
from tests.helpers.identity_fixture import IDENTITIES
from tests.helpers.keycloak import ACME_SCOPE, BOB, KeycloakTokenSource, decode_unverified
from tests.helpers.keycloak_model import ADMIN_CLIENT_ID, ADMIN_CLIENT_SECRET, API_AUDIENCE, SPA_CLIENT_ID

KEYCLOAK = "keycloak"
KEYS = "/api/keys"
ADMIN_ROLE = "/api/access-checks/admin-role"
NO_TENANT = {"detail": "No tenant context"}
DAY = 24 * 60 * 60


@pytest.fixture
def canonical_keycloak(monkeypatch, keycloak_token_source: KeycloakTokenSource) -> None:
    """A deployment that syncs with Keycloak and authorizes its principals from the canonical model."""
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", KEYCLOAK}))
    monkeypatch.setattr(identity, "SYNC_PROVIDER", KEYCLOAK)
    monkeypatch.setenv(ISSUER_VARIABLE, keycloak_token_source.issuer)
    monkeypatch.setenv(ADMIN_CLIENT_ID_VARIABLE, ADMIN_CLIENT_ID)
    monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, ADMIN_CLIENT_SECRET)
    monkeypatch.setenv(API_CLIENT_VARIABLE, API_AUDIENCE)


@pytest_asyncio.fixture(loop_scope="session")
async def admin_api(canonical_keycloak) -> AsyncIterator[KeycloakAdminApi]:
    async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
        yield await connect_keycloak_admin(http)


@dataclass(frozen=True)
class Realm:
    acme: Tenant
    globex: Tenant
    bob_token: str


@pytest_asyncio.fixture(loop_scope="session")
async def realm(db_session, keycloak_token_source: KeycloakTokenSource) -> Realm:
    """The ``keycloak`` provider, ``acme`` and ``globex`` linked to the realm's organizations; bob admin of both."""
    provider = Provider(name=KEYCLOAK, type=ProviderType.oidc, issuer_url=keycloak_token_source.issuer)
    db_session.add(provider)
    await db_session.flush()
    tenants = {}
    for key in ("acme", "globex"):
        tenant = Tenant(
            name=f"{key}-{uuid.uuid4()}",
            external_org_id=key,
            external_org_provider_id=provider.id,
            status=TenantStatus.active,
            domains=[IDENTITIES.tenants[key].domain],
        )
        db_session.add(tenant)
        tenants[key] = tenant
    bob = User(email=IDENTITIES.users[BOB.username].email, user_name=BOB.username, status=UserStatus.active)
    db_session.add(bob)
    await db_session.flush()
    db_session.add(IdPLink(user_id=bob.id, provider_id=provider.id, external_sub=BOB.id, external_email=bob.email))
    admin_id = (await _admin_role(db_session)).id
    for tenant in tenants.values():
        db_session.add(UserTenantRole(user_id=bob.id, tenant_id=tenant.id, role_id=admin_id))
    await db_session.flush()
    bob_token = keycloak_token_source.login(BOB, client_id=SPA_CLIENT_ID, scope=ACME_SCOPE).access_token
    return Realm(acme=tenants["acme"], globex=tenants["globex"], bob_token=bob_token)


async def _admin_role(db_session) -> Role:
    """The global ``admin`` role, created when the database has none."""
    found = (await db_session.execute(select(Role).where(Role.name == "admin", Role.tenant_id.is_(None)))).scalars()
    role = found.first()
    if role is None:
        role = Role(name="admin")
        db_session.add(role)
        await db_session.flush()
    return role


@pytest.fixture
def client(db_session, admin_api, keycloak_token_source: KeycloakTokenSource) -> AsyncClient:
    app = FastAPI()
    app.state.limiter = limiter
    app.state.keycloak_admin = admin_api
    app.add_exception_handler(IdentityErrorRaised, identity_error_raised_handler)
    app.include_router(accesskeys.router, prefix="/api")
    app.include_router(access_checks.router, prefix="/api")

    async def _session():
        yield db_session

    app.dependency_overrides[get_rbac_session] = _session
    keycloak = keycloak_config(keycloak_token_source.issuer, API_AUDIENCE, SPA_CLIENT_ID, require_https=False)
    app.add_middleware(TokenValidationMiddleware, providers=[descope_config(""), keycloak])
    limiter.reset()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _headers(token: str, tenant: Tenant | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if tenant is not None:
        headers[TENANT_SELECTOR_HEADER] = str(tenant.id)
    return headers


def _grant(source: KeycloakTokenSource, client_id: str, secret: str) -> httpx.Response:
    return httpx.post(
        f"{source.issuer}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, secret),
        timeout=10,
    )


def _token(source: KeycloakTokenSource, client_id: str, secret: str) -> str:
    response = _grant(source, client_id, secret)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _registered_client(source: KeycloakTokenSource, client_id: str) -> dict | None:
    """The client as the realm admin reads it, or ``None``."""
    with source._admin() as http:
        found = http.get("/clients", params={"clientId": client_id}).json()
    return found[0] if found else None


@pytest_asyncio.fixture(loop_scope="session")
async def credential(client, realm, keycloak_token_source) -> AsyncIterator[dict]:
    """A credential of acme carrying bob's ``admin`` role, deleted afterwards whatever the outcome."""
    response = await client.post(
        KEYS,
        json={"name": "ci robot", "expire_time": int(time.time()) + DAY, "role_names": ["admin"]},
        headers=_headers(realm.bob_token, realm.acme),
    )
    assert response.status_code == 200, response.text
    created = response.json()
    try:
        yield created
    finally:
        await client.delete(f"{KEYS}/{created['key']['id']}", headers=_headers(realm.bob_token, realm.acme))


class TestACredentialIsAKeycloakClientOfItsTenant:
    async def test_it_is_a_confidential_client_with_a_service_account_and_no_flow(
        self, credential, realm, keycloak_token_source
    ):
        key = credential["key"]
        registered = _registered_client(keycloak_token_source, key["clientId"])

        assert key["clientId"].startswith(KEYCLOAK_MACHINE_CLIENT_PREFIX)
        assert {
            "publicClient": registered["publicClient"],
            "serviceAccountsEnabled": registered["serviceAccountsEnabled"],
            "standardFlowEnabled": registered["standardFlowEnabled"],
            "implicitFlowEnabled": registered["implicitFlowEnabled"],
            "directAccessGrantsEnabled": registered["directAccessGrantsEnabled"],
            "fullScopeAllowed": registered["fullScopeAllowed"],
            CREDENTIAL_ATTRIBUTE: registered["attributes"][CREDENTIAL_ATTRIBUTE],
            TENANT_ATTRIBUTE: registered["attributes"][TENANT_ATTRIBUTE],
            ORGANIZATION_ATTRIBUTE: registered["attributes"][ORGANIZATION_ATTRIBUTE],
        } == {
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "fullScopeAllowed": False,
            CREDENTIAL_ATTRIBUTE: key["id"],
            TENANT_ATTRIBUTE: str(realm.acme.id),
            ORGANIZATION_ATTRIBUTE: "acme",
        }

    async def test_it_is_listed_and_read_in_its_tenant_only(self, client, credential, realm):
        key = credential["key"]

        listed = await client.get(KEYS, headers=_headers(realm.bob_token, realm.acme))
        elsewhere = await client.get(KEYS, headers=_headers(realm.bob_token, realm.globex))
        read = await client.get(f"{KEYS}/{key['id']}", headers=_headers(realm.bob_token, realm.acme))
        read_elsewhere = await client.get(f"{KEYS}/{key['id']}", headers=_headers(realm.bob_token, realm.globex))

        assert {entry["id"]: entry for entry in listed.json()["keys"]}[key["id"]] == key
        assert key["id"] not in {entry["id"] for entry in elsewhere.json()["keys"]}
        assert (read.status_code, read.json()) == (200, key)
        assert read_elsewhere.status_code == 404


class TestItsTokenIsAcceptedInItsTenantOnly:
    async def test_the_token_names_the_api_and_the_client(self, credential, keycloak_token_source):
        token = _token(keycloak_token_source, credential["key"]["clientId"], credential["cleartext"])

        _header, claims = decode_unverified(token)
        assert (claims["aud"], claims["azp"], claims["typ"]) == (API_AUDIENCE, credential["key"]["clientId"], "Bearer")

    async def test_its_tenant_answers_with_its_roles(self, client, credential, keycloak_token_source):
        token = _token(keycloak_token_source, credential["key"]["clientId"], credential["cleartext"])

        response = await client.get(ADMIN_ROLE, headers=_headers(token))

        assert (response.status_code, response.json()) == (200, {"roles": ["admin"]})

    async def test_another_tenant_is_refused(self, client, credential, realm, keycloak_token_source):
        """Bob, who made it, is an admin of globex too: the credential is not."""
        token = _token(keycloak_token_source, credential["key"]["clientId"], credential["cleartext"])

        response = await client.get(ADMIN_ROLE, headers=_headers(token, realm.globex))

        assert (response.status_code, response.json()) == (403, NO_TENANT)


class TestADeactivatedOrDeletedCredentialIsRefused:
    async def test_deactivated_its_token_is_refused_and_no_new_one_is_issued(
        self, client, credential, realm, keycloak_token_source
    ):
        key = credential["key"]
        token = _token(keycloak_token_source, key["clientId"], credential["cleartext"])

        deactivated = await client.post(f"{KEYS}/{key['id']}/deactivate", headers=_headers(realm.bob_token, realm.acme))
        refused = await client.get(ADMIN_ROLE, headers=_headers(token))
        grant = _grant(keycloak_token_source, key["clientId"], credential["cleartext"])

        assert (deactivated.status_code, deactivated.json()) == (200, {"status": "deactivated", "key_id": key["id"]})
        assert (refused.status_code, refused.json()) == (403, NO_TENANT)
        # Keycloak 26.8.2 answers a disabled service account 401 invalid_request ("User ... disabled").
        assert (grant.status_code, grant.json()["error"]) == (401, "invalid_request")

        activated = await client.post(f"{KEYS}/{key['id']}/activate", headers=_headers(realm.bob_token, realm.acme))
        again = _token(keycloak_token_source, key["clientId"], credential["cleartext"])
        accepted = await client.get(ADMIN_ROLE, headers=_headers(again))

        assert (activated.status_code, activated.json()) == (200, {"status": "activated", "key_id": key["id"]})
        assert accepted.status_code == 200

    async def test_deleted_its_token_is_refused_and_its_client_is_gone(
        self, client, credential, realm, keycloak_token_source
    ):
        key = credential["key"]
        token = _token(keycloak_token_source, key["clientId"], credential["cleartext"])

        deleted = await client.delete(f"{KEYS}/{key['id']}", headers=_headers(realm.bob_token, realm.acme))
        refused = await client.get(ADMIN_ROLE, headers=_headers(token))
        grant = _grant(keycloak_token_source, key["clientId"], credential["cleartext"])

        assert (deleted.status_code, deleted.json()) == (200, {"status": "deleted", "key_id": key["id"]})
        assert (refused.status_code, refused.json()) == (403, NO_TENANT)
        assert (grant.status_code, grant.json()["error"]) == (401, "invalid_client")
        assert _registered_client(keycloak_token_source, key["clientId"]) is None


class TestWhatACredentialMayCarry:
    async def test_a_role_the_creator_does_not_hold_is_refused(self, client, realm, keycloak_token_source):
        response = await client.post(
            KEYS,
            json={"name": "escalation", "expire_time": int(time.time()) + DAY, "role_names": ["owner"]},
            headers=_headers(realm.bob_token, realm.acme),
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "Cannot grant roles you do not hold: owner"

    async def test_another_tenants_admin_cannot_touch_it(self, client, credential, realm, keycloak_token_source):
        """Bob's token naming globex reaches only globex's credentials."""
        key = credential["key"]

        response = await client.delete(f"{KEYS}/{key['id']}", headers=_headers(realm.bob_token, realm.globex))

        assert response.status_code == 404
        assert _registered_client(keycloak_token_source, key["clientId"]) is not None
