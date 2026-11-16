"""``GET /api/identity`` and the organization lookup against the ``keycloak-test`` realm.

Every token is issued and signed by the container (``tests/helpers/keycloak.py``)
for the fixture users in ``keycloak/fixtures/README.md``. The app is the shipped
token middleware on the providers the environment configures, so Keycloak's
membership lookup and membership list read the realm's Admin API through the
realm's own read-only service account. The canonical services run on the test
Postgres.
"""

from dataclasses import replace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_identity_provisioning_service, get_identity_resolution_service
from app.dependencies.rbac import MEMBERSHIP_LOOKUP_FAILED
from app.dependencies.tenant_access import SOLE_MEMBERSHIP_NOT_CONFIRMED
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import (
    KeycloakOrganizationLookup,
    KeycloakTenantMemberships,
    ProviderTokenConfig,
    configured_providers,
)
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.routers import access_checks, protected
from app.services.identity_provisioning import IdentityProvisioningService
from app.services.identity_resolution import IdentityResolutionService
from app.services.keycloak_directory import KeycloakDirectory, MemberUnknown
from scripts.seed_keycloak import keycloak_provider
from tests.helpers.keycloak import (
    ALICE,
    BOB,
    DAVE,
    FRANK,
    GLOBEX_SCOPE,
    MEMBER_APP_CLIENT,
    MEMBER_TYPE_CLIENT,
    TENANT_ADMIN_APP_CLIENT,
    TENANT_ADMIN_TYPE_CLIENT,
    KeycloakTokens,
    KeycloakTokenSource,
    decode_unverified,
)
from tests.helpers.keycloak_model import (
    DIRECTORY_CLIENT_ID,
    DIRECTORY_CLIENT_SECRET,
    MODEL_VARIABLES,
    set_model_environment,
)

ACME = "acme"
GLOBEX = "globex"
DOCUMENTS_READ = "documents:read"
SETTINGS_MANAGE = "settings:manage"
IDENTITY = "/api/identity"
SETTINGS_MANAGE_ROUTE = "/api/access-checks/settings-manage"
KEYCLOAK = "Keycloak"
# Nothing listens here: the Admin API is unreachable.
UNREACHABLE_ISSUER = "http://127.0.0.1:9/realms/identity-stack"
DIRECTORY_LOGGER = "app.services.keycloak_directory"
# Well formed, and no user of the realm import has it.
UNKNOWN_MEMBER_ID = "0dead000-0000-4000-8000-0000000000ff"

_PROVIDER_VARIABLES = (
    "DESCOPE_PROJECT_ID",
    "ORY_ISSUER_URL",
    "ORY_AUDIENCE",
    "ORY_REQUIRE_AUDIENCE",
    "ORY_CLIENT_ID",
    "ORY_POST_LOGOUT_REDIRECT_URI",
    "KEYCLOAK_ISSUER_URL",
    "KEYCLOAK_AUDIENCES",
    "KEYCLOAK_REQUIRE_HTTPS",
    "KEYCLOAK_POST_LOGOUT_REDIRECT_URI",
    *MODEL_VARIABLES,
    "FRONTEND_URL",
)


@pytest.fixture(scope="module")
def keycloak(keycloak_token_source) -> KeycloakTokenSource:
    return keycloak_token_source


@pytest.fixture
def environment(monkeypatch, keycloak):
    """A backend configured for the fixture realm, its lookup client included."""
    for name in _PROVIDER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", keycloak.issuer)
    monkeypatch.setenv("KEYCLOAK_AUDIENCES", f"{MEMBER_TYPE_CLIENT},{TENANT_ADMIN_TYPE_CLIENT}")
    monkeypatch.setenv("KEYCLOAK_REQUIRE_HTTPS", "false")
    set_model_environment(monkeypatch, MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT)
    return monkeypatch


def _with_directory(directory: KeycloakDirectory) -> list[ProviderTokenConfig]:
    """The environment's providers, with Keycloak's lookup and membership list reading ``directory``."""
    providers = []
    for provider in configured_providers():
        if provider.name == KEYCLOAK:
            provider = replace(
                provider,
                sole_membership_lookup=KeycloakOrganizationLookup(provider.tenant_claims.model, directory),
                tenant_memberships=KeycloakTenantMemberships(provider.tenant_claims.model, directory),
            )
        providers.append(provider)
    return providers


def _app(db_session, providers: list[ProviderTokenConfig] | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(protected.router, prefix="/api")
    app.include_router(access_checks.router, prefix="/api")

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
    app.add_middleware(TokenValidationMiddleware, providers=providers or configured_providers())
    return app


@pytest_asyncio.fixture(loop_scope="session")
async def registered(db_session, keycloak, environment):
    """The ``keycloak`` provider row, as ``scripts/seed_keycloak.py`` writes it."""
    db_session.add(keycloak_provider(issuer_url=keycloak.issuer))
    await db_session.flush()
    return db_session


async def _get(app: FastAPI, path: str, token: KeycloakTokens) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers={"Authorization": f"Bearer {token.access_token}"})


def _assert(response: httpx.Response, status: int, detail: str) -> None:
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}


# --- Done when 1 and 2 --------------------------------------------------------


async def test_a_first_call_provisions_the_user_and_reports_the_tokens_organization(registered, keycloak):
    token = keycloak.login(ALICE, client_id=MEMBER_APP_CLIENT)

    response = await _get(_app(registered), IDENTITY, token)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == "alice@acme.test"
    assert body["linked_idps"] == [{"provider_name": "keycloak", "external_sub": ALICE.id}]
    assert body["roles"] == []
    assert body["token_tenant"] == {"tenant_id": ACME, "permissions": [DOCUMENTS_READ]}
    assert body["tenant_memberships"] == [{"tenant_id": ACME, "tenant_name": "Acme"}]
    link = await IdPLinkRepository(registered).get_by_provider_name_and_sub("keycloak", ALICE.id)
    assert link is not None
    assert str(link.user_id) == body["user"]["id"]


async def test_a_second_call_resolves_the_same_user(registered, keycloak):
    app = _app(registered)
    token = keycloak.login(ALICE, client_id=MEMBER_APP_CLIENT)

    first = await _get(app, IDENTITY, token)
    second = await _get(app, IDENTITY, token)

    assert second.status_code == 200, second.text
    assert second.json() == first.json()


async def test_the_two_organization_user_sees_both_and_the_one_organization_user_one(registered, keycloak):
    app = _app(registered)

    bob = await _get(app, IDENTITY, keycloak.login(BOB, client_id=MEMBER_APP_CLIENT))
    alice = await _get(app, IDENTITY, keycloak.login(ALICE, client_id=MEMBER_APP_CLIENT))

    assert bob.status_code == 200, bob.text
    assert {m["tenant_id"]: m for m in bob.json()["tenant_memberships"]} == {
        ACME: {"tenant_id": ACME, "tenant_name": "Acme"},
        GLOBEX: {"tenant_id": GLOBEX, "tenant_name": "Globex"},
    }
    assert alice.json()["tenant_memberships"] == [{"tenant_id": ACME, "tenant_name": "Acme"}]


async def test_a_token_for_the_other_organization_reports_that_one(registered, keycloak):
    response = await _get(
        _app(registered), IDENTITY, keycloak.login(BOB, client_id=MEMBER_APP_CLIENT, scope=GLOBEX_SCOPE)
    )

    assert response.status_code == 200, response.text
    assert response.json()["token_tenant"] == {"tenant_id": GLOBEX, "permissions": [DOCUMENTS_READ]}


# --- Done when 3 and 8: the Admin API cannot be asked ---------------------------


async def test_an_unreachable_admin_api_is_502_on_the_endpoint_and_on_a_permission_check(registered, keycloak):
    app = _app(
        registered,
        _with_directory(
            KeycloakDirectory(
                issuer=UNREACHABLE_ISSUER, client_id=DIRECTORY_CLIENT_ID, client_secret=DIRECTORY_CLIENT_SECRET
            )
        ),
    )

    identity = await _get(app, IDENTITY, keycloak.login(ALICE, client_id=MEMBER_APP_CLIENT))
    check = await _get(app, SETTINGS_MANAGE_ROUTE, keycloak.login(DAVE, client_id=TENANT_ADMIN_APP_CLIENT))

    _assert(identity, 502, MEMBERSHIP_LOOKUP_FAILED)
    _assert(check, 502, MEMBERSHIP_LOOKUP_FAILED)


async def test_a_service_account_answered_401_is_502_and_the_log_has_the_status_not_the_secret(
    registered, keycloak, caplog
):
    wrong_secret = "not-the-directory-secret"
    app = _app(
        registered,
        _with_directory(
            KeycloakDirectory(issuer=keycloak.issuer, client_id=DIRECTORY_CLIENT_ID, client_secret=wrong_secret)
        ),
    )

    with caplog.at_level("WARNING", logger=DIRECTORY_LOGGER):
        identity = await _get(app, IDENTITY, keycloak.login(ALICE, client_id=MEMBER_APP_CLIENT))
        check = await _get(app, SETTINGS_MANAGE_ROUTE, keycloak.login(DAVE, client_id=TENANT_ADMIN_APP_CLIENT))

    _assert(identity, 502, MEMBERSHIP_LOOKUP_FAILED)
    _assert(check, 502, MEMBERSHIP_LOOKUP_FAILED)
    messages = [record.getMessage() for record in caplog.records]
    assert any("401" in r.getMessage() for r in caplog.records if r.name == DIRECTORY_LOGGER), messages
    assert not any(wrong_secret in message or DIRECTORY_CLIENT_SECRET in message for message in messages)


async def test_a_service_account_answered_403_is_502_and_the_log_has_the_status(registered, keycloak, caplog):
    member = keycloak.login(ALICE, client_id=MEMBER_APP_CLIENT)
    admin = keycloak.login(DAVE, client_id=TENANT_ADMIN_APP_CLIENT)

    with keycloak.directory_without("view-organizations"), caplog.at_level("WARNING", logger=DIRECTORY_LOGGER):
        app = _app(registered)
        identity = await _get(app, IDENTITY, member)
        check = await _get(app, SETTINGS_MANAGE_ROUTE, admin)

    _assert(identity, 502, MEMBERSHIP_LOOKUP_FAILED)
    _assert(check, 502, MEMBERSHIP_LOOKUP_FAILED)
    messages = [record.getMessage() for record in caplog.records if record.name == DIRECTORY_LOGGER]
    assert any("403" in message for message in messages), messages
    assert not any(DIRECTORY_CLIENT_SECRET in message for message in messages)


# --- Done when 9: the service account reads and writes nothing else -------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/organizations", {"name": "Initech", "alias": "initech", "domains": [{"name": "initech.test"}]}),
        ("POST", "/organizations/{globex}/members", ALICE.id),
        ("PUT", "/organizations/{globex}", {"alias": GLOBEX, "name": "Globex", "attributes": {"x": ["y"]}}),
        ("POST", "/users/{carol}/role-mappings/clients/{member}", [{"name": DOCUMENTS_READ}]),
    ],
    ids=["create-organization", "add-member", "edit-organization", "map-client-role"],
)
def test_the_service_account_is_refused_every_write(keycloak, method, path, body):
    admin = httpx.Client(
        base_url=f"{keycloak.base_url}/admin/realms/{keycloak.realm}",
        headers={"Authorization": f"Bearer {keycloak.admin_access_token()}"},
    )
    (globex,) = [o for o in admin.get("/organizations").json() if o["alias"] == GLOBEX]
    (member,) = admin.get("/clients", params={"clientId": MEMBER_TYPE_CLIENT}).json()
    carol = "ca401000-0000-4000-8000-000000000003"
    if path.endswith("{member}"):
        (role,) = [r for r in admin.get(f"/clients/{member['id']}/roles").json() if r["name"] == DOCUMENTS_READ]
        body = [role]
    service_account = httpx.Client(
        base_url=admin.base_url, headers={"Authorization": f"Bearer {keycloak.directory_token()}"}
    )

    response = service_account.request(
        method, path.format(globex=globex["id"], carol=carol, member=member["id"]), json=body
    )

    assert response.status_code == 403, response.text


async def test_a_member_the_realm_does_not_know_is_answered_404_and_typed_apart(keycloak):
    """A well-formed ``sub`` of a user deleted from the realm: Keycloak answers 404, the lookup no organizations."""
    directory = KeycloakDirectory(
        issuer=keycloak.issuer, client_id=DIRECTORY_CLIENT_ID, client_secret=DIRECTORY_CLIENT_SECRET
    )

    with pytest.raises(MemberUnknown):
        await directory.organizations_of(UNKNOWN_MEMBER_ID)


def test_the_realm_keeps_email_verified_meaningful_for_cross_provider_linking(keycloak):
    """Provisioning links a Keycloak identity to a canonical user on a verified email (docs/security.md).

    So the realm brokers no other identity provider (whose email it could take
    on trust), lets nobody register, keeps emails unique, and verifies an
    email before it counts: a changed email is not ``email_verified`` until the
    user proves it.
    """
    admin = httpx.Client(
        base_url=f"{keycloak.base_url}/admin/realms/{keycloak.realm}",
        headers={"Authorization": f"Bearer {keycloak.admin_access_token()}"},
    )
    realm = admin.get("").json()

    assert admin.get("/identity-provider/instances").json() == []
    assert {name: realm.get(name) for name in ("registrationAllowed", "duplicateEmailsAllowed", "verifyEmail")} == {
        "registrationAllowed": False,
        "duplicateEmailsAllowed": False,
        "verifyEmail": True,
    }


# --- Done when 10 and the owner's case on #559 ---------------------------------


async def test_the_single_organization_tenant_admin_is_allowed_and_the_two_organization_one_refused(
    registered, keycloak
):
    app = _app(registered)
    dave = keycloak.login(DAVE, client_id=TENANT_ADMIN_APP_CLIENT)
    bob = keycloak.login(BOB, client_id=TENANT_ADMIN_APP_CLIENT)

    dave_check = await _get(app, SETTINGS_MANAGE_ROUTE, dave)
    dave_identity = await _get(app, IDENTITY, dave)
    bob_check = await _get(app, SETTINGS_MANAGE_ROUTE, bob)
    bob_identity = await _get(app, IDENTITY, bob)

    assert dave_check.status_code == 200, dave_check.text
    assert dave_check.json() == {"tenant_id": ACME, "permission": SETTINGS_MANAGE}
    assert dave_identity.status_code == 200, dave_identity.text
    assert dave_identity.json()["token_tenant"]["tenant_id"] == ACME
    assert SETTINGS_MANAGE in dave_identity.json()["token_tenant"]["permissions"]
    _assert(bob_check, 403, SOLE_MEMBERSHIP_NOT_CONFIRMED)
    _assert(bob_identity, 403, SOLE_MEMBERSHIP_NOT_CONFIRMED)


async def test_a_tenant_admin_who_moves_to_another_organization_is_not_its_tenant_admin(registered, keycloak):
    """Admin of acme; added to globex as a member; removed from acme. The client roles stay; globex grants nothing."""
    app = _app(registered)
    before = await _get(app, SETTINGS_MANAGE_ROUTE, keycloak.login(FRANK, client_id=TENANT_ADMIN_APP_CLIENT))
    assert before.status_code == 200, before.text

    with keycloak.moved(FRANK, out_of=ACME, into=GLOBEX):
        token = keycloak.login(FRANK, client_id=TENANT_ADMIN_APP_CLIENT, scope=GLOBEX_SCOPE)
        assert decode_unverified(token.access_token)[1]["organization"] == [GLOBEX]
        check = await _get(app, SETTINGS_MANAGE_ROUTE, token)
        identity = await _get(app, IDENTITY, token)

    _assert(check, 403, SOLE_MEMBERSHIP_NOT_CONFIRMED)
    _assert(identity, 403, SOLE_MEMBERSHIP_NOT_CONFIRMED)
