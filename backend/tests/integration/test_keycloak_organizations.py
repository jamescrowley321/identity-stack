"""Keycloak organizations as tenants and client roles as permissions, with realm-signed tokens.

Every token is issued and signed by the ``keycloak-test`` container, through the
realm's authorization-code flow (``tests/helpers/keycloak.py``), for the fixture
users in ``keycloak/fixtures/README.md``. The app is the shipped token middleware
on the provider list the environment configures, and the shipped permission-gated
routes.

The membership lookup an organization-bound token needs is not part of this
backend yet. Where a test needs one it uses ``_Lookup``, a stated test double.
"""

from dataclasses import dataclass, replace

import httpx
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.dependencies.rbac import INSUFFICIENT_PERMISSIONS, MEMBERSHIP_LOOKUP_FAILED, require_permission
from app.dependencies.tenant import get_tenant_id
from app.dependencies.tenant_access import NO_TENANT_CONTEXT, SOLE_MEMBERSHIP_NOT_CONFIRMED
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import ProviderTokenConfig, configured_providers
from app.routers import access_checks
from tests.helpers.keycloak import (
    ALICE,
    BOB,
    BOTH_ORGANIZATIONS_SCOPE,
    DAVE,
    ERIN,
    GLOBEX_SCOPE,
    MEMBER_APP_CLIENT,
    MEMBER_TYPE_CLIENT,
    TENANT_ADMIN_APP_CLIENT,
    TENANT_ADMIN_TYPE_CLIENT,
    KeycloakTokens,
    KeycloakTokenSource,
    decode_unverified,
)
from tests.helpers.keycloak_model import MODEL_VARIABLES, set_model_environment

ACME = "acme"
GLOBEX = "globex"
DOCUMENTS_READ = "documents:read"
DOCUMENTS_WRITE = "documents:write"
SETTINGS_MANAGE = "settings:manage"
DOCUMENTS_READ_ROUTE = "/api/access-checks/documents-read"
SETTINGS_MANAGE_ROUTE = "/api/access-checks/settings-manage"
OWN_WRITE_ROUTE = "/own/documents-write"
KEYCLOAK = "Keycloak"

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


@dataclass(frozen=True)
class OrganizationTokens:
    """One login per case, taken once for the module."""

    bob_member_acme: KeycloakTokens
    bob_member_globex: KeycloakTokens
    bob_member_both: KeycloakTokens
    alice_member_acme: KeycloakTokens
    alice_asking_for_globex: KeycloakTokens
    erin_member_acme: KeycloakTokens
    bob_admin_acme: KeycloakTokens
    bob_admin_globex: KeycloakTokens
    dave_admin_acme: KeycloakTokens


@pytest.fixture(scope="module")
def keycloak(keycloak_token_source) -> KeycloakTokenSource:
    return keycloak_token_source


@pytest.fixture(scope="module")
def tokens(keycloak) -> OrganizationTokens:
    return OrganizationTokens(
        bob_member_acme=keycloak.login(BOB, client_id=MEMBER_APP_CLIENT),
        bob_member_globex=keycloak.login(BOB, client_id=MEMBER_APP_CLIENT, scope=GLOBEX_SCOPE),
        bob_member_both=keycloak.login(BOB, client_id=MEMBER_APP_CLIENT, scope=BOTH_ORGANIZATIONS_SCOPE),
        alice_member_acme=keycloak.login(ALICE, client_id=MEMBER_APP_CLIENT),
        alice_asking_for_globex=keycloak.login(ALICE, client_id=MEMBER_APP_CLIENT, scope=GLOBEX_SCOPE),
        erin_member_acme=keycloak.login(ERIN, client_id=MEMBER_APP_CLIENT),
        bob_admin_acme=keycloak.login(BOB, client_id=TENANT_ADMIN_APP_CLIENT),
        bob_admin_globex=keycloak.login(BOB, client_id=TENANT_ADMIN_APP_CLIENT, scope=GLOBEX_SCOPE),
        dave_admin_acme=keycloak.login(DAVE, client_id=TENANT_ADMIN_APP_CLIENT),
    )


@pytest.fixture
def environment(monkeypatch, keycloak):
    """A backend configured for the fixture realm, with Keycloak as its only provider with an issuer."""
    for name in _PROVIDER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", keycloak.issuer)
    monkeypatch.setenv("KEYCLOAK_AUDIENCES", f"{MEMBER_TYPE_CLIENT},{TENANT_ADMIN_TYPE_CLIENT}")
    monkeypatch.setenv("KEYCLOAK_REQUIRE_HTTPS", "false")
    set_model_environment(monkeypatch, MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT)
    return monkeypatch


class _Lookup:
    """A test double of the membership lookup: a canned list of organizations or an error."""

    def __init__(self, organizations: list[str] | None = None, *, error: Exception | None = None) -> None:
        self.organizations = organizations
        self.error = error
        self.subjects: list[str] = []

    async def __call__(self, claims: dict) -> list[str]:
        self.subjects.append(claims["sub"])
        if self.error is not None:
            raise self.error
        return self.organizations


def _providers(lookup: _Lookup | None) -> list[ProviderTokenConfig]:
    """The environment's providers; with ``lookup``, Keycloak's membership lookup is the double."""
    providers = configured_providers()
    if lookup is None:
        return providers
    return [replace(p, sole_membership_lookup=lookup) if p.name == KEYCLOAK else p for p in providers]


def _app(lookup: _Lookup | None) -> FastAPI:
    app = FastAPI()
    app.include_router(access_checks.router, prefix="/api")

    @app.get(OWN_WRITE_ROUTE, dependencies=[Depends(require_permission(DOCUMENTS_WRITE))])
    async def documents_write(tenant_id: str = Depends(get_tenant_id)):
        return {"tenant_id": tenant_id}

    app.add_middleware(TokenValidationMiddleware, providers=_providers(lookup))
    return app


async def _get(path: str, token: KeycloakTokens, *, lookup: _Lookup | None = None) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=_app(lookup)), base_url="http://test") as client:
        return await client.get(path, headers={"Authorization": f"Bearer {token.access_token}"})


def _claims(token: KeycloakTokens) -> dict:
    _header, claims = decode_unverified(token.access_token)
    return claims


def _roles(token: KeycloakTokens) -> list[str]:
    claims = _claims(token)
    return sorted(claims["resource_access"][claims["aud"]]["roles"])


def _assert_403(response: httpx.Response, detail: str) -> None:
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": detail}


# --- Done when 4: the organization is the tenant ---------------------------------


async def test_the_two_organization_users_member_tokens_give_each_organization(environment, tokens):
    assert _claims(tokens.bob_member_acme)["organization"] == [ACME]
    assert _claims(tokens.bob_member_globex)["organization"] == [GLOBEX]

    acme = await _get(DOCUMENTS_READ_ROUTE, tokens.bob_member_acme)
    globex = await _get(DOCUMENTS_READ_ROUTE, tokens.bob_member_globex)

    assert acme.status_code == 200, acme.text
    assert acme.json() == {"tenant_id": ACME, "permission": DOCUMENTS_READ}
    assert globex.status_code == 200, globex.text
    assert globex.json() == {"tenant_id": GLOBEX, "permission": DOCUMENTS_READ}


async def test_a_token_requested_with_two_organization_scopes_is_refused(environment, tokens):
    claims = _claims(tokens.bob_member_both)
    assert sorted(claims["organization"]) == [ACME, GLOBEX]
    assert _roles(tokens.bob_member_both) == [DOCUMENTS_READ]

    _assert_403(await _get(DOCUMENTS_READ_ROUTE, tokens.bob_member_both), NO_TENANT_CONTEXT)


async def test_a_token_with_no_organization_claim_is_refused(environment, tokens):
    """``alice`` is not in ``globex``: Keycloak issues her a token and leaves the claim out."""
    claims = _claims(tokens.alice_asking_for_globex)
    assert "organization" not in claims
    assert claims["aud"] == MEMBER_TYPE_CLIENT
    assert DOCUMENTS_READ in _roles(tokens.alice_asking_for_globex)

    _assert_403(await _get(DOCUMENTS_READ_ROUTE, tokens.alice_asking_for_globex), NO_TENANT_CONTEXT)


# --- Done when 5: client roles are the permissions --------------------------------


async def test_a_member_holding_documents_read_is_answered(environment, tokens):
    response = await _get(DOCUMENTS_READ_ROUTE, tokens.alice_member_acme)

    assert response.status_code == 200, response.text
    assert response.json() == {"tenant_id": ACME, "permission": DOCUMENTS_READ}


async def test_the_member_without_documents_read_is_refused(environment, tokens):
    assert _claims(tokens.erin_member_acme)["organization"] == [ACME]
    assert _roles(tokens.erin_member_acme) == [DOCUMENTS_WRITE]

    _assert_403(await _get(DOCUMENTS_READ_ROUTE, tokens.erin_member_acme), INSUFFICIENT_PERMISSIONS)


@pytest.mark.parametrize("holder", ["alice_member_acme", "erin_member_acme"])
async def test_a_member_token_carrying_documents_write_is_refused_on_a_route_needing_it(environment, tokens, holder):
    """``member`` is portable and ``documents:write`` is not on its portable list."""
    token = getattr(tokens, holder)
    assert DOCUMENTS_WRITE in _roles(token)

    _assert_403(await _get(OWN_WRITE_ROUTE, token), INSUFFICIENT_PERMISSIONS)


async def test_a_member_token_does_not_reach_the_settings_manage_route(environment, tokens):
    _assert_403(await _get(SETTINGS_MANAGE_ROUTE, tokens.alice_member_acme), INSUFFICIENT_PERMISSIONS)


# --- Done when 7: organization-bound, no lookup wired -----------------------------


@pytest.mark.parametrize("holder", ["bob_admin_acme", "bob_admin_globex", "dave_admin_acme"])
async def test_every_tenant_admin_token_is_refused_with_no_lookup_wired(environment, tokens, holder):
    token = getattr(tokens, holder)
    claims = _claims(token)
    assert claims["aud"] == TENANT_ADMIN_TYPE_CLIENT
    assert claims["azp"] == TENANT_ADMIN_APP_CLIENT
    assert SETTINGS_MANAGE in _roles(token)

    _assert_403(await _get(SETTINGS_MANAGE_ROUTE, token), SOLE_MEMBERSHIP_NOT_CONFIRMED)


# --- Done when 8: organization-bound, against the lookup double --------------------


@pytest.mark.parametrize(("holder", "alias"), [("bob_admin_acme", ACME), ("bob_admin_globex", GLOBEX)])
async def test_the_two_organization_tenant_admin_is_refused_in_both(environment, tokens, holder, alias):
    token = getattr(tokens, holder)
    assert _claims(token)["organization"] == [alias]
    lookup = _Lookup([ACME, GLOBEX])

    _assert_403(await _get(SETTINGS_MANAGE_ROUTE, token, lookup=lookup), SOLE_MEMBERSHIP_NOT_CONFIRMED)
    assert lookup.subjects == [_claims(token)["sub"]]


async def test_the_single_organization_tenant_admin_is_allowed(environment, tokens):
    lookup = _Lookup([ACME])

    response = await _get(SETTINGS_MANAGE_ROUTE, tokens.dave_admin_acme, lookup=lookup)

    assert response.status_code == 200, response.text
    assert response.json() == {"tenant_id": ACME, "permission": SETTINGS_MANAGE}
    assert lookup.subjects == [_claims(tokens.dave_admin_acme)["sub"]]


async def test_a_lookup_that_raises_is_502_and_no_grant(environment, tokens):
    lookup = _Lookup(error=RuntimeError("directory unreachable"))

    response = await _get(SETTINGS_MANAGE_ROUTE, tokens.dave_admin_acme, lookup=lookup)

    assert response.status_code == 502
    assert response.json() == {"detail": MEMBERSHIP_LOOKUP_FAILED}


async def test_a_member_token_is_decided_without_the_lookup(environment, tokens):
    lookup = _Lookup(error=RuntimeError("directory unreachable"))

    response = await _get(DOCUMENTS_READ_ROUTE, tokens.bob_member_acme, lookup=lookup)

    assert response.status_code == 200, response.text
    assert lookup.subjects == []
