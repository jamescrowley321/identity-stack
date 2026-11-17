"""The whole Keycloak path, from Chromium, against the shipped SPA and backend.

The realm is the ``keycloak-test`` import (``keycloak/fixtures/README.md``). The
backend runs from ``app.main`` with the realm's token model: ``member`` portable
with ``documents:read`` only, ``tenant-admin`` organization-bound. Each case is
one item of identity-stack#562's scope.
"""

import secrets

import pytest
from playwright.sync_api import Page, Response, expect

from tests.e2e_keycloak.browser_login import BrowserLogin
from tests.helpers.keycloak import (
    ACME_SCOPE,
    ALICE,
    BOB,
    CAROL,
    DAVE,
    ERIN,
    GLOBEX_SCOPE,
    KEYCLOAK_BASE_URL,
    MEMBER_APP_CLIENT,
    TENANT_ADMIN_APP_CLIENT,
    KeycloakTokenSource,
    decode_unverified,
)

ACME = "acme"
GLOBEX = "globex"
ACME_NAME = "Acme"
GLOBEX_NAME = "Globex"
DOCUMENTS_READ = "documents:read"
SETTINGS_MANAGE = "settings:manage"

IDENTITY = "/api/identity"
DOCUMENTS_READ_ROUTE = "/api/access-checks/documents-read"
SETTINGS_MANAGE_ROUTE = "/api/access-checks/settings-manage"

INSUFFICIENT_PERMISSIONS = {"detail": "Insufficient permissions"}
NO_TENANT_CONTEXT = {"detail": "No tenant context"}
SOLE_MEMBERSHIP_NOT_CONFIRMED = {"detail": "This tenant is not confirmed as the caller's only membership"}
TOKEN_REFUSED = {"detail": "Invalid or expired token"}

# Paths the middleware lets through without a token (app/middleware/factory.py).
UNAUTHENTICATED_PATHS = {"/api/health", "/api/validate-id-token", "/docs", "/redoc", "/openapi.json"}
UNAUTHENTICATED_PREFIXES = ("/api/internal/",)

# oidc-client-ts keeps the signed-in user in sessionStorage under this prefix.
_READ_ACCESS_TOKEN = """() => {
  const key = Object.keys(sessionStorage).find((name) => name.startsWith("oidc.user:"));
  return key ? JSON.parse(sessionStorage.getItem(key)).access_token : null;
}"""


def claims(token: str) -> dict:
    _header, payload = decode_unverified(token)
    return payload


def _spa_access_token(page: Page) -> str:
    token = page.evaluate(_READ_ACCESS_TOKEN)
    assert token, "the SPA holds no signed-in user"
    return token


def _sign_in_to_the_spa(page: Page, frontend_url: str, user) -> None:
    """Sign In on the SPA's login page, then Keycloak's identity-first form."""
    page.goto(f"{frontend_url}/login")
    page.get_by_role("button", name="Sign In").click()
    page.locator('input[name="username"]').fill(user.username)
    page.locator('input[name="username"]').press("Enter")
    page.locator('input[name="password"]').fill(user.password)
    page.locator('input[name="password"]').press("Enter")
    page.wait_for_url(f"{frontend_url}/")


@pytest.fixture
def token_for(new_context, browser_login: BrowserLogin):
    """An access token from a fresh browser login of ``user`` through ``client_id`` with ``scope``."""

    def _login(user, client_id: str, scope: str) -> str:
        return browser_login.authorize(new_context(), user, client_id=client_id, scope=scope).access_token

    return _login


def _identity(api, token: str) -> dict:
    response = api(token).get(IDENTITY)
    assert response.status == 200, response.text()
    return response.json()


def _answer(response) -> tuple[int, dict]:
    return response.status, response.json()


# --- Scope 1-5: the member of both organizations, through the SPA ---------------


def test_the_two_organization_member_logs_in_switches_organization_and_logs_out(
    page: Page, frontend_url: str, api, keycloak: KeycloakTokenSource
):
    # 1. Log in from the SPA.
    _sign_in_to_the_spa(page, frontend_url, BOB)
    tenant = page.locator("header").get_by_role("combobox")
    expect(tenant).to_have_text(ACME_NAME)
    acme_token = _spa_access_token(page)
    assert claims(acme_token)["organization"] == [ACME]

    # 2. GET /api/identity: the user, the token's tenant and its permissions.
    identity = _identity(api, acme_token)
    assert identity["user"]["email"] == "bob@globex.test"
    assert identity["linked_idps"] == [{"provider_name": "keycloak", "external_sub": BOB.id}]
    assert identity["token_tenant"] == {"tenant_id": ACME, "permissions": [DOCUMENTS_READ]}
    assert {m["tenant_id"]: m["tenant_name"] for m in identity["tenant_memberships"]} == {
        ACME: ACME_NAME,
        GLOBEX: GLOBEX_NAME,
    }

    # 3. The documents:read route answers; the settings:manage one is refused.
    assert _answer(api(acme_token).get(DOCUMENTS_READ_ROUTE)) == (
        200,
        {"tenant_id": ACME, "permission": DOCUMENTS_READ},
    )
    assert _answer(api(acme_token).get(SETTINGS_MANAGE_ROUTE)) == (403, INSUFFICIENT_PERMISSIONS)

    # 4. Switch organization. Keycloak answers the new authorize request on the
    # live session with redirects only: no page of its own, so no login form.
    keycloak_pages: list[tuple[int, str]] = []

    def _record(response: Response) -> None:
        if response.request.is_navigation_request() and response.url.startswith(KEYCLOAK_BASE_URL):
            keycloak_pages.append((response.status, response.url))

    page.on("response", _record)
    tenant.click()
    with page.expect_navigation(url=lambda url: url.startswith(f"{frontend_url}/?") and "code=" in url):
        page.get_by_role("option", name=GLOBEX_NAME).click()
    expect(tenant).to_have_text(GLOBEX_NAME)
    page.remove_listener("response", _record)

    assert keycloak_pages, "the switch did not go through Keycloak"
    assert [status for status, _url in keycloak_pages if not 300 <= status < 400] == [], keycloak_pages
    globex_token = _spa_access_token(page)
    assert claims(globex_token)["organization"] == [GLOBEX]
    assert claims(globex_token)["sid"] == claims(acme_token)["sid"]
    assert _identity(api, globex_token)["token_tenant"] == {"tenant_id": GLOBEX, "permissions": [DOCUMENTS_READ]}
    assert _answer(api(globex_token).get(DOCUMENTS_READ_ROUTE)) == (
        200,
        {"tenant_id": GLOBEX, "permission": DOCUMENTS_READ},
    )

    # 5. Log out: Keycloak ends the session and the next sign-in asks for the login form.
    session = claims(globex_token)["sid"]
    assert session in keycloak.session_ids(BOB)
    page.locator("header").get_by_role("button").last.click()
    page.get_by_role("menuitem", name="Sign out").click()
    page.wait_for_url(f"{frontend_url}/login")

    assert session not in keycloak.session_ids(BOB)
    assert page.evaluate(_READ_ACCESS_TOKEN) is None
    page.get_by_role("button", name="Sign In").click()
    expect(page.locator('input[name="username"]')).to_be_visible()
    assert page.url.startswith(KEYCLOAK_BASE_URL)


# --- Scope 6: no client role, no aud, 401 everywhere -----------------------------


def _authenticated_operations(api) -> list[tuple[str, str]]:
    """Every method and path in the shipped app's OpenAPI document that needs a token."""
    response = api().get("/openapi.json")
    assert response.status == 200, response.text()
    operations = [
        (method.upper(), path)
        for path, item in response.json()["paths"].items()
        if path not in UNAUTHENTICATED_PATHS and not path.startswith(UNAUTHENTICATED_PREFIXES)
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert (("GET", IDENTITY) in operations) and (("GET", DOCUMENTS_READ_ROUTE) in operations), operations
    return operations


def test_a_user_with_no_client_role_is_refused_on_every_protected_route(token_for, api):
    token = token_for(CAROL, MEMBER_APP_CLIENT, ACME_SCOPE)
    assert "aud" not in claims(token)
    assert claims(token)["organization"] == [ACME]

    refused = {}
    for method, path in _authenticated_operations(api):
        response = api(token).fetch(path.replace("{", "x-").replace("}", ""), method=method)
        refused[(method, path)] = _answer(response)

    assert refused == dict.fromkeys(refused, (401, TOKEN_REFUSED))


# --- Scope 7: tenant-admin across organizations ---------------------------------


@pytest.mark.parametrize("scope", [ACME_SCOPE, GLOBEX_SCOPE], ids=[ACME, GLOBEX])
def test_the_tenant_admin_of_two_organizations_is_refused_in_each(token_for, api, scope):
    token = token_for(BOB, TENANT_ADMIN_APP_CLIENT, scope)
    assert set(claims(token)["resource_access"]["tenant-admin"]["roles"]) == {
        "members:invite",
        "members:remove",
        SETTINGS_MANAGE,
    }

    assert _answer(api(token).get(SETTINGS_MANAGE_ROUTE)) == (403, SOLE_MEMBERSHIP_NOT_CONFIRMED)
    assert _answer(api(token).get(IDENTITY)) == (403, SOLE_MEMBERSHIP_NOT_CONFIRMED)


def test_the_tenant_admin_of_one_organization_is_allowed_there(token_for, api):
    token = token_for(DAVE, TENANT_ADMIN_APP_CLIENT, ACME_SCOPE)

    assert _answer(api(token).get(SETTINGS_MANAGE_ROUTE)) == (200, {"tenant_id": ACME, "permission": SETTINGS_MANAGE})
    assert _identity(api, token)["token_tenant"]["tenant_id"] == ACME


# --- Scope 8: a user outside globex asks for it ----------------------------------


def test_a_user_outside_globex_is_served_nothing_in_globex(new_context, browser_login: BrowserLogin, api):
    context = new_context()
    acme_token = browser_login.authorize(context, ALICE, client_id=MEMBER_APP_CLIENT, scope=ACME_SCOPE).access_token
    assert _identity(api, acme_token)["tenant_memberships"] == [{"tenant_id": ACME, "tenant_name": ACME_NAME}]

    # Asked on her live session, Keycloak asks for her password again, then
    # issues a token that names no organization.
    verifier = secrets.token_urlsafe(48)
    authorization = browser_login.authorization(
        context, ALICE, client_id=MEMBER_APP_CLIENT, scope=GLOBEX_SCOPE, verifier=verifier
    )
    assert authorization.forms_shown == ("password",)
    asked = browser_login.exchange(
        context, authorization.code, client_id=MEMBER_APP_CLIENT, verifier=verifier
    ).access_token
    assert "organization" not in claims(asked)
    assert _answer(api(asked).get(DOCUMENTS_READ_ROUTE)) == (403, NO_TENANT_CONTEXT)
    assert api(asked).get(IDENTITY).status == 403

    # Naming globex in the header does not move her acme token there.
    selecting = api(acme_token, {"X-Tenant-ID": GLOBEX})
    assert _answer(selecting.get(DOCUMENTS_READ_ROUTE)) == (200, {"tenant_id": ACME, "permission": DOCUMENTS_READ})
    assert _identity(api, acme_token)["token_tenant"]["tenant_id"] == ACME


# --- Scope 9: a write permission does not travel on a portable type ---------------


def test_documents_write_is_not_granted_in_either_organization(token_for, api, keycloak: KeycloakTokenSource):
    # alice holds documents:read and documents:write; for this test she is in both organizations.
    with keycloak.joined(ALICE, GLOBEX):
        for scope, organization in ((ACME_SCOPE, ACME), (GLOBEX_SCOPE, GLOBEX)):
            token = token_for(ALICE, MEMBER_APP_CLIENT, scope)
            assert set(claims(token)["resource_access"]["member"]["roles"]) == {DOCUMENTS_READ, "documents:write"}

            assert _identity(api, token)["token_tenant"] == {"tenant_id": organization, "permissions": [DOCUMENTS_READ]}

    # erin's one client role is documents:write: her member token grants nothing.
    token = token_for(ERIN, MEMBER_APP_CLIENT, ACME_SCOPE)
    assert claims(token)["resource_access"]["member"]["roles"] == ["documents:write"]
    assert _identity(api, token)["token_tenant"] == {"tenant_id": ACME, "permissions": []}
    assert _answer(api(token).get(DOCUMENTS_READ_ROUTE)) == (403, INSUFFICIENT_PERMISSIONS)
