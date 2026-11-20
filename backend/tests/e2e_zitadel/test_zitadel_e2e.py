"""The whole Zitadel path, from Chromium, against the shipped SPA and backend.

The instance is the compose Zitadel with ``infra/zitadel`` applied and
``make zitadel-seed`` run, both from the shared fixture
(``infra/fixtures/test-identities.json``): the canonical tenants linked to their
organizations, the fixture's two-tenant user linked beforehand with their roles,
every other user unknown to the canonical store, and the unlinked-tenant user in
an organization no tenant names. Each test is one item
of identity-stack#570's scope; the machine-user token is in
``tests/integration_zitadel/test_zitadel_machine_token.py``.
"""

import re

import pytest
from playwright.sync_api import Page, Request, expect

from tests.e2e_zitadel.browser_login import LOGIN_NAME_FIELD, PASSWORD_FIELD, BrowserLogin, sign_in
from tests.e2e_zitadel.conftest import NEWCOMER, Database, Instance
from tests.helpers.identity_fixture import IDENTITIES, FixtureUser

# Linked by ``make zitadel-seed`` beforehand, with their fixture role in each tenant.
LINKED = IDENTITIES.cases.two_tenants
# In an organization no canonical tenant names.
UNLINKED = IDENTITIES.cases.unlinked_tenant
TENANT_ORGANIZATIONS = sorted(key for key, tenant in IDENTITIES.tenants.items() if tenant.canonical)

IDENTITY = "/api/identity"
VALIDATE_ID_TOKEN = "/api/validate-id-token"
ADMIN_ROUTE = "/api/access-checks/admin-role"
VIEWER_ROUTE = "/api/access-checks/viewer-role"

INSUFFICIENT_PERMISSIONS = {"detail": "Insufficient permissions"}
NOT_LINKED = {"detail": "Your organization is not linked to a tenant"}
TOKEN_REFUSED = {"detail": "Invalid or expired token"}
# What Login V2 shows instead of the register form when the policy refuses.
REGISTRATION_DISABLED = "The registration is disabled"
REGISTER_FORM_FIELD = '[data-testid="email-text-input"]'
# The scopes the SPA asks for (infra/zitadel frontend_environment).
SPA_SCOPE = "openid profile email offline_access urn:zitadel:iam:user:resourceowner"

# A Login V2 page a sign-in can start on: the remembered accounts, the login name
# or the password.
_LOGIN_V2_PAGE = re.compile(r"/ui/v2/login/(accounts|loginname|password)\?")

# oidc-client-ts keeps the signed-in user in sessionStorage under this prefix.
_READ_SIGNED_IN_USER = """() => {
  const key = Object.keys(sessionStorage).find((name) => name.startsWith("oidc.user:"));
  return key ? JSON.parse(sessionStorage.getItem(key)) : null;
}"""
_STORED_TOKEN_KEYS = """() => [sessionStorage, localStorage].flatMap((store) =>
  Object.keys(store).filter((name) => name.startsWith("oidc."))
)"""


def _tenant_where(user: FixtureUser, role: str) -> str:
    """The one tenant where the fixture gives ``user`` ``role``."""
    (tenant,) = [tenant for tenant, held in user.roles.items() if held == role]
    return tenant


def _signed_in_user(page: Page) -> dict:
    user = page.evaluate(_READ_SIGNED_IN_USER)
    assert user, "the SPA holds no signed-in user"
    return user


def _sign_in_to_the_spa(page: Page, frontend_url: str, user) -> dict:
    """Sign In on the SPA's login page, then Login V2's forms; return the SPA's signed-in user."""
    page.goto(f"{frontend_url}/login")
    page.get_by_role("button", name="Sign In").click()
    sign_in(page, user, frontend_url)
    return _signed_in_user(page)


def _answer(response) -> tuple[int, dict]:
    return response.status, response.json()


def _tenant_switcher(page: Page):
    return page.locator("header").get_by_role("combobox")


def _switch_tenant(page: Page, frontend_url: str, name: str, tenant_id: str) -> None:
    """Pick ``name`` in the SPA's switcher; the SPA's next API call must name ``tenant_id``."""
    switcher = _tenant_switcher(page)
    if switcher.inner_text() != name:
        switcher.click()
        page.get_by_role("option", name=name).click()
    expect(switcher).to_have_text(name)

    def _names_the_tenant(request: Request) -> bool:
        return "/api/" in request.url and request.headers.get("x-tenant-id") == tenant_id

    with page.expect_request(_names_the_tenant):
        page.goto(f"{frontend_url}/")
    expect(switcher).to_have_text(name)


# --- 1. First login ----------------------------------------------------------------


def test_a_first_login_creates_a_member_of_the_home_tenant_only(
    page: Page, frontend_url: str, api, zitadel: Instance, database: Database
):
    email, home = NEWCOMER.email, NEWCOMER.home
    # The organization claim decides the first grant: `member`, the fixture's role for this user.
    # Checked before the sign-in, which uses up the fresh seed.
    assert NEWCOMER.roles == {home: "member"}
    assert database.user_ids_with_email(email) == [], f"{NEWCOMER.key} exists already: run against a fresh seed"

    token = _sign_in_to_the_spa(page, frontend_url, zitadel.users[NEWCOMER.key])["access_token"]

    identity = api(token).get(IDENTITY)
    assert identity.status == 200, identity.text()
    body = identity.json()
    tenant_id = database.tenant_ids()[home]
    assert body["user"]["email"] == email
    assert database.user_ids_with_email(email) == [body["user"]["id"]]
    assert body["tenant_memberships"] == [
        {
            "tenant_id": tenant_id,
            "tenant_name": home,
            "status": "active",
            "external_org_id": zitadel.organization_ids[home],
        }
    ]
    assert body["roles"] == [{"tenant_id": tenant_id, "role_name": "member", "permissions": []}]
    assert body["linked_idps"] == [{"provider_name": "zitadel", "external_sub": zitadel.user_ids[NEWCOMER.key]}]
    expect(page.locator("header").get_by_text(home, exact=True)).to_be_visible()


# --- 2. Roles and tenant switch ------------------------------------------------------


def test_the_linked_user_is_admin_in_one_tenant_and_viewer_after_switching_to_the_other(
    page: Page, frontend_url: str, api, zitadel: Instance, database: Database
):
    admin_in, viewer_in = _tenant_where(LINKED, "admin"), _tenant_where(LINKED, "viewer")
    token = _sign_in_to_the_spa(page, frontend_url, zitadel.users[LINKED.key])["access_token"]
    tenants = database.tenant_ids()

    _switch_tenant(page, frontend_url, admin_in, tenants[admin_in])
    as_admin = api(token, {"X-Tenant-ID": tenants[admin_in]})
    assert _answer(as_admin.get(ADMIN_ROUTE)) == (200, {"roles": ["admin"]})

    _switch_tenant(page, frontend_url, viewer_in, tenants[viewer_in])
    as_viewer = api(token, {"X-Tenant-ID": tenants[viewer_in]})
    assert _answer(as_viewer.get(ADMIN_ROUTE)) == (403, INSUFFICIENT_PERMISSIONS)
    assert _answer(as_viewer.get(VIEWER_ROUTE)) == (200, {"roles": ["viewer"]})


# --- 3. Unmapped organization --------------------------------------------------------


def test_a_user_of_an_organization_no_tenant_names_gets_nothing(
    page: Page, frontend_url: str, api, zitadel: Instance, database: Database
):
    assert not IDENTITIES.tenants[UNLINKED.home].canonical
    token = _sign_in_to_the_spa(page, frontend_url, zitadel.users[UNLINKED.key])["access_token"]

    assert _answer(api(token).get(IDENTITY)) == (403, NOT_LINKED)
    assert database.user_ids_with_email(UNLINKED.email) == []
    expect(page.locator("header").get_by_text("No tenants", exact=True)).to_be_visible()
    # Descope's tenant settings do not serve a Zitadel principal: the page is not there (#678).
    page.goto(f"{frontend_url}/settings")
    expect(page).to_have_url(re.compile(rf"^{re.escape(frontend_url)}/?$"))
    expect(page.locator("[data-sidebar=sidebar]").get_by_role("link", name="Tenant Settings")).to_have_count(0)


# --- 4. Self-registration ------------------------------------------------------------


@pytest.mark.parametrize("organization", TENANT_ORGANIZATIONS)
def test_registration_in_a_tenant_organization_is_refused(page: Page, zitadel: Instance, organization):
    page.goto(f"{zitadel.issuer}/ui/v2/login/register?organization={zitadel.organization_ids[organization]}")

    expect(page.get_by_text(REGISTRATION_DISABLED)).to_be_visible()
    expect(page.locator(REGISTER_FORM_FIELD)).to_have_count(0)


# --- 5. Token binding, live ----------------------------------------------------------


def test_a_live_id_token_is_refused(page: Page, frontend_url: str, api, zitadel: Instance):
    signed_in = _sign_in_to_the_spa(page, frontend_url, zitadel.users[LINKED.key])
    assert _answer(api(signed_in["access_token"]).get(IDENTITY))[0] == 200

    assert _answer(api(signed_in["id_token"]).get(IDENTITY)) == (401, TOKEN_REFUSED)


def test_the_spas_live_id_token_is_validated_by_the_instance(page: Page, frontend_url: str, api, zitadel: Instance):
    signed_in = _sign_in_to_the_spa(page, frontend_url, zitadel.users[LINKED.key])

    response = api(signed_in["id_token"]).post(VALIDATE_ID_TOKEN)

    assert response.status == 200, response.text()
    body = response.json()
    assert (body["iss"], body["azp"], zitadel.spa_client_id in body["aud"]) == (
        zitadel.issuer,
        zitadel.spa_client_id,
        True,
    )
    assert _answer(api(signed_in["access_token"]).post(VALIDATE_ID_TOKEN)) == (401, TOKEN_REFUSED)


def test_a_live_token_from_the_second_application_is_refused(
    new_context, browser_login: BrowserLogin, api, zitadel: Instance
):
    tokens = browser_login.authorize(
        new_context(), zitadel.users[LINKED.key], client_id=zitadel.other_client_id, scope=SPA_SCOPE
    )

    assert _answer(api(tokens.access_token).get(IDENTITY)) == (401, TOKEN_REFUSED)


# --- 6. Logout -----------------------------------------------------------------------


def test_logout_drops_the_tokens_and_the_next_sign_in_asks_for_credentials(
    page: Page, frontend_url: str, api, zitadel: Instance, measured
):
    signed_in = _sign_in_to_the_spa(page, frontend_url, zitadel.users[LINKED.key])
    assert signed_in.get("refresh_token"), "the SPA got no refresh token (offline_access)"

    # The user menu's trigger is the header button holding the avatar.
    header = page.locator("header")
    header.get_by_role("button").filter(has=page.locator("[data-slot='avatar']")).click()
    # The SPA leaves for Zitadel's end_session endpoint, which sends it back to /login.
    with page.expect_request(lambda request: request.url.startswith(f"{zitadel.issuer}/oidc/v1/end_session")):
        page.get_by_role("menuitem", name="Sign out").click()
    page.wait_for_url(f"{frontend_url}/login")
    page.get_by_role("button", name="Sign In").wait_for()

    # 1. The SPA holds no tokens.
    assert page.evaluate(_STORED_TOKEN_KEYS) == []

    # 2. A protected page leads to Zitadel's login form, which asks for credentials.
    page.goto(f"{frontend_url}/")
    page.wait_for_url(f"{frontend_url}/login")
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_url(_LOGIN_V2_PAGE)
    first_page = _LOGIN_V2_PAGE.search(page.url).group(1)
    measured("first Login V2 page after logout", first_page)
    if first_page == "accounts":
        # Login V2 keeps the login names it has seen in its own cookie and offers
        # them; choosing one still asks for the password.
        page.get_by_role("button", name=zitadel.users[LINKED.key].login_name).click()
    expect(page.locator(f"{LOGIN_NAME_FIELD}, {PASSWORD_FIELD}")).to_be_visible()
    assert page.url.startswith(zitadel.issuer)

    # 3. Recorded, not asserted: whether the old tokens are still accepted.
    refreshed = page.context.request.post(
        f"{zitadel.issuer}/oauth/v2/token",
        form={
            "grant_type": "refresh_token",
            "refresh_token": signed_in["refresh_token"],
            "client_id": zitadel.spa_client_id,
        },
    )
    measured("old refresh token at the token endpoint after logout", f"HTTP {refreshed.status}")
    old_access = api(signed_in["access_token"]).get(IDENTITY)
    measured("old access token at GET /api/identity after logout", f"HTTP {old_access.status}")
