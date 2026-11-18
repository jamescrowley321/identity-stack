"""Keycloak authorized from the canonical model, against ``keycloak-test`` and the test Postgres.

Every token is issued and signed by the container through the realm's
authorization-code flow (``tests/helpers/keycloak.py``). It goes through the
shipped middleware on the shipped Keycloak config, and the tenant and the
roles come from canonical rows in Postgres through the shipped dependencies:
``GET /api/identity`` provisions a first sign-in, and the ``/api/access-checks``
routes answer what the canonical store grants. Nothing is stubbed. The unit
suite covers the branches (``test_keycloak_first_login.py``,
``test_keycloak_canonical_authorization.py``, ``test_keycloak_token_binding.py``).

A first sign-in whose email is unverified cannot be produced here: the realm
has ``verifyEmail`` on, so Keycloak does not finish such a login.
"""

import uuid
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.dependencies import rbac
from app.dependencies.identity import get_identity_provisioning_service, get_identity_resolution_service
from app.dependencies.rbac import get_rbac_session
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import descope_config, keycloak_config
from app.middleware.rate_limit import limiter
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import IdPLink, User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.routers import access_checks, protected
from app.routers.protected import canonical_identity
from app.services.identity_provisioning import IdentityProvisioningService
from app.services.identity_resolution import IdentityResolutionService
from tests.helpers.identity_fixture import IDENTITIES
from tests.helpers.keycloak import ACME_SCOPE, ALICE, BOB, TARGET, KeycloakTokens, KeycloakTokenSource
from tests.helpers.keycloak_model import API_AUDIENCE, OTHER_CLIENT_ID, SPA_CLIENT_ID

IDENTITY = "/api/identity"
ADMIN_ROLE = "/api/access-checks/admin-role"
VIEWER_ROLE = "/api/access-checks/viewer-role"
UNIFORM_401 = {"detail": "Invalid or expired token"}
NO_TENANT = {"detail": "No tenant context"}
ORGANIZATION_NOT_LINKED = {"detail": protected.ORGANIZATION_NOT_LINKED}
KEYCLOAK = "keycloak"

GRACE = TARGET.users[IDENTITIES.cases.unlinked_tenant.key]
OPENID_SCOPE = "openid"
UNLINKED_SCOPE = "openid organization:unlinked"
BOTH_SCOPE = "openid organization:acme organization:globex"


def _email(username: str) -> str:
    return IDENTITIES.users[username].email


@dataclass(frozen=True)
class Tokens:
    """One login per case, taken once for the module."""

    alice_acme: KeycloakTokens
    alice_no_organization: KeycloakTokens
    bob_acme: KeycloakTokens
    bob_both: KeycloakTokens
    grace_unlinked: KeycloakTokens
    bob_other_client: KeycloakTokens
    other_realm_access_token: str


@pytest.fixture(scope="module")
def tokens(keycloak_token_source: KeycloakTokenSource) -> Tokens:
    source = keycloak_token_source
    return Tokens(
        alice_acme=source.login(ALICE, client_id=SPA_CLIENT_ID, scope=ACME_SCOPE),
        alice_no_organization=source.login(ALICE, client_id=SPA_CLIENT_ID, scope=OPENID_SCOPE),
        bob_acme=source.login(BOB, client_id=SPA_CLIENT_ID, scope=ACME_SCOPE),
        bob_both=source.login(BOB, client_id=SPA_CLIENT_ID, scope=BOTH_SCOPE),
        grace_unlinked=source.login(GRACE, client_id=SPA_CLIENT_ID, scope=UNLINKED_SCOPE),
        bob_other_client=source.login(BOB, client_id=OTHER_CLIENT_ID, scope=ACME_SCOPE),
        other_realm_access_token=source.admin_access_token(),
    )


@pytest.fixture
def canonical_keycloak(monkeypatch):
    """A deployment that lists ``keycloak`` for canonical authorization and first-login provisioning."""
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", KEYCLOAK}))
    monkeypatch.setattr(protected, "JIT_ENABLED_PROVIDERS", frozenset({"ory", KEYCLOAK}))


@dataclass(frozen=True)
class Realm:
    provider: Provider
    acme: Tenant
    globex: Tenant


@pytest_asyncio.fixture(loop_scope="session")
async def realm(db_session) -> Realm:
    """The ``keycloak`` provider row and the tenants ``make keycloak-seed`` links to the realm's aliases."""
    provider = Provider(name=KEYCLOAK, type=ProviderType.oidc, issuer_url="unused-by-these-tests")
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
    await db_session.flush()
    return Realm(provider=provider, acme=tenants["acme"], globex=tenants["globex"])


@pytest.fixture
def client(db_session, keycloak_token_source) -> AsyncClient:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_api_route(IDENTITY, canonical_identity, methods=["GET"])
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

    async def _provisioner() -> IdentityProvisioningService:
        return IdentityProvisioningService(
            session=db_session,
            provider_repository=ProviderRepository(db_session),
            idp_link_repository=IdPLinkRepository(db_session),
            user_repository=UserRepository(db_session),
            tenant_repository=TenantRepository(db_session),
            role_repository=RoleRepository(db_session),
            assignment_repository=UserTenantRoleRepository(db_session),
        )

    app.dependency_overrides[get_identity_resolution_service] = _resolver
    app.dependency_overrides[get_identity_provisioning_service] = _provisioner
    app.dependency_overrides[get_rbac_session] = _session
    keycloak = keycloak_config(keycloak_token_source.issuer, API_AUDIENCE, SPA_CLIENT_ID, require_https=False)
    app.add_middleware(TokenValidationMiddleware, providers=[descope_config(""), keycloak])
    limiter.reset()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _bearer(token: str, tenant: Tenant | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if tenant is not None:
        headers[TENANT_SELECTOR_HEADER] = str(tenant.id)
    return headers


async def _global_role(db_session, name: str) -> Role:
    role = (await db_session.execute(select(Role).where(Role.name == name, Role.tenant_id.is_(None)))).scalars().first()
    if role is None:
        role = Role(name=name)
        db_session.add(role)
        await db_session.flush()
    return role


async def _linked_user(db_session, realm: Realm, username: str, *, status: UserStatus = UserStatus.active) -> User:
    """A canonical user linked to the fixture user's Keycloak id, as an operator or the seed links one."""
    keycloak_user = TARGET.users[username]
    user = User(email=_email(username), user_name=username, status=status)
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        IdPLink(
            user_id=user.id,
            provider_id=realm.provider.id,
            external_sub=keycloak_user.id,
            external_email=user.email,
        )
    )
    await db_session.flush()
    return user


async def _assign(db_session, user: User, tenant: Tenant, role_name: str) -> None:
    role = await _global_role(db_session, role_name)
    db_session.add(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
    await db_session.flush()


async def _user_by_email(db_session, email: str) -> User | None:
    return (await db_session.execute(select(User).where(User.email == email))).scalars().first()


# --- the token binding ---------------------------------------------------------


class TestOnlyTheSpasAccessTokenIsAccepted:
    async def test_the_spas_access_token(self, canonical_keycloak, realm, client, db_session, tokens):
        await _linked_user(db_session, realm, BOB.username)

        response = await client.get(IDENTITY, headers=_bearer(tokens.bob_acme.access_token))

        assert response.status_code == 200, response.text

    @pytest.mark.parametrize(
        "token",
        [
            lambda tokens: tokens.bob_acme.id_token,
            lambda tokens: tokens.bob_acme.refresh_token,
            lambda tokens: tokens.bob_other_client.access_token,
            lambda tokens: tokens.other_realm_access_token,
        ],
        ids=["its id token", "a refresh token", "another client's token (no API audience)", "another issuer's"],
    )
    @pytest.mark.parametrize("path", [IDENTITY, VIEWER_ROLE])
    async def test_every_other_token_is_401(self, canonical_keycloak, realm, client, db_session, tokens, token, path):
        await _linked_user(db_session, realm, BOB.username)

        response = await client.get(path, headers=_bearer(token(tokens)))

        assert (response.status_code, response.json()) == (401, UNIFORM_401)


# --- first sign-in ---------------------------------------------------------------


class TestFirstSignIn:
    async def test_one_mapped_alias_provisions_a_member_of_its_tenant(
        self, canonical_keycloak, realm, client, db_session, tokens
    ):
        response = await client.get(IDENTITY, headers=_bearer(tokens.alice_acme.access_token))

        assert response.status_code == 200, response.text
        user = await _user_by_email(db_session, _email(ALICE.username))
        assert user is not None
        links = (await db_session.execute(select(IdPLink).where(IdPLink.user_id == user.id))).scalars().all()
        assert [(link.provider_id, link.external_sub) for link in links] == [(realm.provider.id, ALICE.id)]
        assignments = (
            await db_session.execute(
                select(UserTenantRole.tenant_id, Role.name)
                .join(Role, Role.id == UserTenantRole.role_id)
                .where(UserTenantRole.user_id == user.id)
            )
        ).all()
        assert [tuple(row) for row in assignments] == [(realm.acme.id, "member")]
        body = response.json()
        assert body["tenant_memberships"] == [{"tenant_id": str(realm.acme.id), "tenant_name": realm.acme.name}]

        # The provisioned member is authorized from those rows.
        viewer = await client.get(VIEWER_ROLE, headers=_bearer(tokens.alice_acme.access_token))
        admin = await client.get(ADMIN_ROLE, headers=_bearer(tokens.alice_acme.access_token))
        assert viewer.json() == {"roles": ["member"]}
        assert admin.status_code == 403

    @pytest.mark.parametrize(
        ("token", "username"),
        [
            (lambda tokens: tokens.alice_no_organization.access_token, ALICE.username),
            (lambda tokens: tokens.bob_both.access_token, BOB.username),
            (lambda tokens: tokens.grace_unlinked.access_token, GRACE.username),
        ],
        ids=["no organization claim", "two aliases", "an alias no tenant names"],
    )
    async def test_is_refused_and_writes_nothing(
        self, canonical_keycloak, realm, client, db_session, tokens, token, username
    ):
        response = await client.get(IDENTITY, headers=_bearer(token(tokens)))

        assert (response.status_code, response.json()) == (403, ORGANIZATION_NOT_LINKED)
        assert await _user_by_email(db_session, _email(username)) is None


# --- canonical authorization -------------------------------------------------------


class TestAUserInTwoTenants:
    async def test_is_tenant_admin_in_each_where_postgres_says_so(
        self, canonical_keycloak, realm, client, db_session, tokens
    ):
        """``bob``'s token names ``acme`` only; the header selects either tenant."""
        bob = await _linked_user(db_session, realm, BOB.username)
        await _assign(db_session, bob, realm.acme, "admin")
        await _assign(db_session, bob, realm.globex, "admin")
        token = tokens.bob_acme.access_token

        for tenant in (realm.acme, realm.globex):
            response = await client.get(ADMIN_ROLE, headers=_bearer(token, tenant))
            assert (response.status_code, response.json()) == (200, {"roles": ["admin"]})
        unselected = await client.get(ADMIN_ROLE, headers=_bearer(token))
        assert (unselected.status_code, unselected.json()) == (403, NO_TENANT)

    async def test_is_admin_only_where_postgres_says_so(self, canonical_keycloak, realm, client, db_session, tokens):
        bob = await _linked_user(db_session, realm, BOB.username)
        await _assign(db_session, bob, realm.acme, "viewer")
        await _assign(db_session, bob, realm.globex, "admin")
        token = tokens.bob_acme.access_token

        in_acme = await client.get(ADMIN_ROLE, headers=_bearer(token, realm.acme))
        in_globex = await client.get(ADMIN_ROLE, headers=_bearer(token, realm.globex))

        assert in_acme.status_code == 403
        assert (in_globex.status_code, in_globex.json()) == (200, {"roles": ["admin"]})


class TestADeactivatedCanonicalUser:
    @pytest.mark.parametrize("status", [UserStatus.inactive, UserStatus.provisioned])
    async def test_is_refused_with_a_valid_token(self, canonical_keycloak, realm, client, db_session, tokens, status):
        bob = await _linked_user(db_session, realm, BOB.username, status=status)
        await _assign(db_session, bob, realm.acme, "admin")

        response = await client.get(ADMIN_ROLE, headers=_bearer(tokens.bob_acme.access_token, realm.acme))

        assert (response.status_code, response.json()) == (403, NO_TENANT)


def test_the_realm_keeps_email_verified_meaningful(keycloak_token_source):
    """First sign-in refuses an unverified email (docs/security.md), so the realm must keep the flag honest.

    It brokers no other identity provider (whose email it could take on trust),
    lets nobody register, keeps emails unique, and verifies an email before it
    counts: a changed email is not ``email_verified`` until the user proves it.
    """
    source = keycloak_token_source
    with httpx.Client(
        base_url=f"{source.base_url}/admin/realms/{source.realm}",
        headers={"Authorization": f"Bearer {source.admin_access_token()}"},
    ) as admin:
        realm = admin.get("").json()
        brokers = admin.get("/identity-provider/instances").json()

    assert brokers == []
    assert {name: realm.get(name) for name in ("registrationAllowed", "duplicateEmailsAllowed", "verifyEmail")} == {
        "registrationAllowed": False,
        "duplicateEmailsAllowed": False,
        "verifyEmail": True,
    }
