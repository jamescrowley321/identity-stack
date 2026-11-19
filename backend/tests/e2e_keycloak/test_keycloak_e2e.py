"""The whole Keycloak path, from Chromium, against the shipped SPA and backend.

The realm is the one ``infra/keycloak/`` builds in ``keycloak-test``, with the
users, organizations and role groups of ``keycloak/fixtures/README.md``; ``make
test-e2e-keycloak`` points the suite at it from the root's outputs. The backend
runs from ``app.main`` with Keycloak's canonical model: the token says who the
caller is, Postgres what the caller may do in the tenant ``X-Tenant-ID`` names.
It syncs with the realm (``SYNC_PROVIDER=keycloak``), so the event pull runs,
and this suite's set-up (``conftest.py``) has taken the realm's role assignments
in (``tests/e2e_keycloak/realm.py::take_in_the_realm``).
"""

import time
from collections.abc import Callable

import httpx
import pytest
from playwright.sync_api import Page, Request, expect

from tests.e2e_keycloak.browser_login import ORGANIZATION, PASSWORD, USERNAME, BrowserLogin, sign_in
from tests.e2e_keycloak.conftest import BACKEND_URL
from tests.helpers.identity_fixture import IDENTITIES
from tests.helpers.keycloak import (
    ALICE,
    BOB,
    ERIN,
    FRANK,
    KEYCLOAK_BASE_URL,
    KeycloakTokenSource,
    decode_unverified,
)
from tests.helpers.keycloak_model import API_AUDIENCE, OTHER_CLIENT_ID, SPA_CLIENT_ID, SPA_SCOPE

ACME = "acme"
GLOBEX = "globex"
DOCUMENTS_READ = "documents:read"
SETTINGS_MANAGE = "settings:manage"

IDENTITY = "/api/identity"
VALIDATE_ID_TOKEN = "/api/validate-id-token"
DOCUMENTS_READ_ROUTE = "/api/access-checks/documents-read"
SETTINGS_MANAGE_ROUTE = "/api/access-checks/settings-manage"

INSUFFICIENT_PERMISSIONS = {"detail": "Insufficient permissions"}
NO_TENANT_CONTEXT = {"detail": "No tenant context"}
TOKEN_REFUSED = {"detail": "Invalid or expired token"}

# Paths the middleware lets through without a token (app/middleware/factory.py).
UNAUTHENTICATED_PATHS = {"/api/health", "/api/validate-id-token", "/docs", "/redoc", "/openapi.json"}
UNAUTHENTICATED_PREFIXES = ("/api/internal/",)

# A console change reaches the backend within one poll (KEYCLOAK_PULL_INTERVAL_SECONDS,
# 10 s by default); an answer that has not changed within this long has not been pulled.
PULL_DEADLINE_SECONDS = 45
_RETRY_SECONDS = 1

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


@pytest.fixture
def token_for(new_context, browser_login: BrowserLogin):
    """An access token from a fresh browser login of ``user`` through the SPA's client, with its scope."""

    def _login(user, *, client_id: str = SPA_CLIENT_ID, organization: str = ACME) -> str:
        return browser_login.authorize(
            new_context(), user, client_id=client_id, scope=SPA_SCOPE, organization=organization
        ).access_token

    return _login


def _identity(api, token: str) -> dict:
    response = api(token).get(IDENTITY)
    assert response.status == 200, response.text()
    return response.json()


# Reconciliation gives a linked tenant its organization's name: the fixture tenant's name.
TENANT_KEYS = {tenant.name: key for key, tenant in IDENTITIES.tenants.items()}


def _tenant_ids(identity: dict) -> dict[str, str]:
    """Canonical tenant id by fixture tenant key."""
    return {
        TENANT_KEYS.get(membership["tenant_name"], membership["tenant_name"]): membership["tenant_id"]
        for membership in identity["tenant_memberships"]
    }


@pytest.fixture(scope="module")
def tenant_ids(browser, browser_login: BrowserLogin) -> dict[str, str]:
    """Canonical tenant id by tenant name, from the two-tenant user's memberships (both fixture tenants)."""
    context = browser.new_context()
    try:
        token = browser_login.authorize(
            context, BOB, client_id=SPA_CLIENT_ID, scope=SPA_SCOPE, organization=ACME
        ).access_token
    finally:
        context.close()
    response = httpx.get(f"{BACKEND_URL}{IDENTITY}", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert response.status_code == 200, response.text
    ids = _tenant_ids(response.json())
    assert set(ids) == {ACME, GLOBEX}, f"bob is not in both tenants ({ids}): run `make keycloak-seed` first"
    return ids


def _answer(response) -> tuple[int, dict]:
    return response.status, response.json()


def _in(api, token: str, tenant_id: str, path: str) -> tuple[int, dict]:
    return _answer(api(token, {"X-Tenant-ID": tenant_id}).get(path))


def _until(answer: Callable[[], object], expected: object) -> object:
    """``answer()`` once it is ``expected``, or its last value when ``PULL_DEADLINE_SECONDS`` have passed."""
    deadline = time.monotonic() + PULL_DEADLINE_SECONDS
    while True:
        got = answer()
        if got == expected or time.monotonic() >= deadline:
            return got
        time.sleep(_RETRY_SECONDS)


# --- The two-tenant user switches tenant in the SPA ------------------------------


def test_the_two_tenant_user_signs_in_switches_tenant_without_a_new_login_and_signs_out(
    page: Page, frontend_url: str, api, keycloak: KeycloakTokenSource, tenant_ids
):
    # bob: admin in acme, viewer in globex (infra/fixtures/test-identities.json).
    two_tenants = IDENTITIES.cases.two_tenants
    assert two_tenants.roles == {ACME: "admin", GLOBEX: "viewer"}

    # 1. Sign in from the SPA. Asked for the plain organization scope, Keycloak
    # asks a member of two organizations which one.
    page.goto(f"{frontend_url}/login")
    page.get_by_role("button", name="Sign In").click()
    assert sign_in(page, BOB, frontend_url, organization=ACME) == [USERNAME, ORGANIZATION, PASSWORD]
    token = _spa_access_token(page)
    assert (claims(token)["azp"], claims(token)["organization"], API_AUDIENCE in claims(token)["aud"]) == (
        SPA_CLIENT_ID,
        [ACME],
        True,
    )
    assert "resource_access" not in claims(token)

    # 2. GET /api/identity: the user and both tenants, whatever the token's organization.
    identity = _identity(api, token)
    assert identity["user"]["email"] == two_tenants.email
    assert identity["linked_idps"] == [{"provider_name": "keycloak", "external_sub": BOB.id}]
    assert _tenant_ids(identity) == tenant_ids

    # 3. The same token, two tenants, the grants Postgres holds in each.
    assert _in(api, token, tenant_ids[ACME], SETTINGS_MANAGE_ROUTE) == (
        200,
        {"tenant_id": tenant_ids[ACME], "permission": SETTINGS_MANAGE},
    )
    assert _in(api, token, tenant_ids[GLOBEX], SETTINGS_MANAGE_ROUTE) == (403, INSUFFICIENT_PERMISSIONS)
    assert _in(api, token, tenant_ids[GLOBEX], DOCUMENTS_READ_ROUTE) == (
        200,
        {"tenant_id": tenant_ids[GLOBEX], "permission": DOCUMENTS_READ},
    )

    # 4. Switch tenant in the SPA, both ways: the next API calls name the tenant,
    # nothing goes to Keycloak and the token stays the same.
    keycloak_requests: list[str] = []

    def _record(request: Request) -> None:
        if request.url.startswith(KEYCLOAK_BASE_URL):
            keycloak_requests.append(request.url)

    page.on("request", _record)
    switcher = page.locator("header").get_by_role("combobox")
    names = {key: IDENTITIES.tenants[key].name for key in (ACME, GLOBEX)}
    for key in (GLOBEX, ACME) if switcher.inner_text() == names[ACME] else (ACME, GLOBEX):
        name = names[key]

        def _names(request: Request, key=key) -> bool:
            return "/api/" in request.url and request.headers.get("x-tenant-id") == tenant_ids[key]

        switcher.click()
        with page.expect_request(_names):
            page.get_by_role("option", name=name).click()
        expect(switcher).to_have_text(name)
    page.remove_listener("request", _record)

    assert keycloak_requests == []
    assert _spa_access_token(page) == token

    # 5. Sign out: Keycloak ends the session and the next sign-in asks for the login form.
    session = claims(token)["sid"]
    assert session in keycloak.session_ids(BOB)
    page.locator("header").get_by_role("button").last.click()
    page.get_by_role("menuitem", name="Sign out").click()
    page.wait_for_url(f"{frontend_url}/login")

    assert session not in keycloak.session_ids(BOB)
    assert page.evaluate(_READ_ACCESS_TOKEN) is None
    page.get_by_role("button", name="Sign In").click()
    expect(page.locator('input[name="username"]')).to_be_visible()
    assert page.url.startswith(KEYCLOAK_BASE_URL)


# --- Another client's token: no API audience, 401 everywhere -----------------------


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


def test_another_clients_token_is_refused_on_every_protected_route(token_for, api, tenant_ids):
    token = token_for(BOB, client_id=OTHER_CLIENT_ID)
    assert (claims(token)["azp"], "aud" in claims(token)) == (OTHER_CLIENT_ID, False)

    refused = {}
    for method, path in _authenticated_operations(api):
        response = api(token, {"X-Tenant-ID": tenant_ids[ACME]}).fetch(
            path.replace("{", "x-").replace("}", ""), method=method
        )
        refused[(method, path)] = _answer(response)

    assert refused == dict.fromkeys(refused, (401, TOKEN_REFUSED))


# --- The SPA's ID token validates at POST /api/validate-id-token ---------------------


def test_the_spas_id_token_is_validated_by_the_realm(
    new_context, browser_login: BrowserLogin, api, keycloak: KeycloakTokenSource
):
    tokens = browser_login.authorize(new_context(), BOB, client_id=SPA_CLIENT_ID, scope=SPA_SCOPE, organization=ACME)

    response = api(tokens.id_token).post(VALIDATE_ID_TOKEN)

    assert response.status == 200, response.text()
    body = response.json()
    assert {name: body[name] for name in ("iss", "aud", "azp")} == {
        "iss": keycloak.issuer,
        "aud": SPA_CLIENT_ID,
        "azp": SPA_CLIENT_ID,
    }
    assert _answer(api(tokens.access_token).post(VALIDATE_ID_TOKEN)) == (401, TOKEN_REFUSED)


# --- A tenant admin of two tenants is admin in each --------------------------------


def test_a_tenant_admin_of_two_tenants_is_admin_in_each(token_for, api, keycloak: KeycloakTokenSource, tenant_ids):
    # frank: admin in acme only, until he is added to globex's admin group in the console.
    assert IDENTITIES.cases.moving_admin.roles == {ACME: "admin"}
    token = token_for(FRANK)
    granted = {name: (200, {"tenant_id": tenant_ids[name], "permission": SETTINGS_MANAGE}) for name in (ACME, GLOBEX)}

    assert _in(api, token, tenant_ids[ACME], SETTINGS_MANAGE_ROUTE) == granted[ACME]
    assert _in(api, token, tenant_ids[GLOBEX], SETTINGS_MANAGE_ROUTE)[0] == 403

    with keycloak.joined(FRANK, GLOBEX, role="admin"):
        assert (
            _until(lambda: _in(api, token, tenant_ids[GLOBEX], SETTINGS_MANAGE_ROUTE), granted[GLOBEX])
            == granted[GLOBEX]
        )
        assert {name: _in(api, token, tenant_ids[name], SETTINGS_MANAGE_ROUTE) for name in (ACME, GLOBEX)} == granted
        assert _tenant_ids(_identity(api, token)) == tenant_ids

    # Out of globex in the console: out of it in the backend after the next poll.
    assert _until(lambda: _in(api, token, tenant_ids[GLOBEX], SETTINGS_MANAGE_ROUTE)[0], 403) == 403
    assert _in(api, token, tenant_ids[ACME], SETTINGS_MANAGE_ROUTE) == granted[ACME]


# --- A user disabled in the console is refused after the next poll -------------------


def test_a_user_disabled_in_keycloak_is_refused_after_the_next_poll(
    token_for, api, keycloak: KeycloakTokenSource, tenant_ids
):
    token = token_for(ERIN)
    served = (200, {"tenant_id": tenant_ids[ACME], "permission": DOCUMENTS_READ})

    def _read() -> tuple[int, dict]:
        return _in(api, token, tenant_ids[ACME], DOCUMENTS_READ_ROUTE)

    assert _read() == served

    with keycloak.disabled(ERIN):
        # The token is still signed and unexpired; only the pulled status refuses it.
        assert _until(lambda: _read()[0], 403) == 403
        # A deactivated user holds no tenant: every tenant-scoped route answers as for no tenant.
        assert _read() == (403, NO_TENANT_CONTEXT)
        assert _identity(api, token)["user"]["status"] == "inactive"

    # Enabled again in the console: served again after the next poll.
    assert _until(_read, served) == served


# --- A user outside a tenant is served nothing there --------------------------------


def test_a_user_outside_globex_is_served_nothing_there(token_for, api, tenant_ids):
    token = token_for(ALICE)
    assert _tenant_ids(_identity(api, token)) == {ACME: tenant_ids[ACME]}

    assert _in(api, token, tenant_ids[ACME], DOCUMENTS_READ_ROUTE) == (
        200,
        {"tenant_id": tenant_ids[ACME], "permission": DOCUMENTS_READ},
    )
    assert _in(api, token, tenant_ids[GLOBEX], DOCUMENTS_READ_ROUTE)[0] == 403
