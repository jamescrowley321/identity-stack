"""Every fixture user signs in through the SPA and gets exactly what the shared fixture gives them.

The fixture (``infra/fixtures/test-identities.json``) says, for each user, which
tenants they belong to and their canonical role in each. ``infra/keycloak`` builds
the realm from it, and the suite's set-up has taken the realm in
(``conftest.py``). For each user, signed in from the SPA's own Sign In:

- a user in no canonical tenant (only in an organization no tenant names) is
  refused ``GET /api/identity`` and the SPA offers no tenant;
- any other user's ``GET /api/identity`` lists exactly their canonical tenants,
  with their fixture role in each and none where they hold none;
- in each of those tenants, named by ``X-Tenant-ID``, the user is granted
  ``documents:read`` and ``settings:manage`` exactly as their role there grants
  them;
- in the SPA, with each of their tenants picked in the tenant selector (a
  member of one tenant sees it named, with no selector), the
  dashboard shows their role there and the sidebar shows the tenant
  administration pages (Members) exactly where the role is an administrator's.
"""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_keycloak.browser_login import sign_in
from tests.helpers.identity_fixture import IDENTITIES, FixtureUser
from tests.helpers.keycloak import TARGET

IDENTITY = "/api/identity"
DOCUMENTS_READ = "documents:read"
SETTINGS_MANAGE = "settings:manage"
# The access-check route for each permission (app/routers/access_checks.py).
ACCESS_CHECKS = {
    DOCUMENTS_READ: "/api/access-checks/documents-read",
    SETTINGS_MANAGE: "/api/access-checks/settings-manage",
}
INSUFFICIENT_PERMISSIONS = {"detail": "Insufficient permissions"}
NOT_LINKED = {"detail": "Your organization is not linked to a tenant"}
# The canonical roles that grant each checked permission (the seeded role
# composites, keycloak/fixtures/README.md); no role grants neither.
GRANTED_BY = {
    DOCUMENTS_READ: frozenset({"owner", "admin", "member", "viewer"}),
    SETTINGS_MANAGE: frozenset({"owner", "admin"}),
}
# The roles whose holder sees the tenant administration pages in the sidebar.
ADMINISTRATOR_ROLES = frozenset({"owner", "admin"})
ADMINISTRATION_PAGE = "Members"
NO_TENANTS = "No tenants"

# oidc-client-ts keeps the signed-in user in sessionStorage under this prefix.
_READ_ACCESS_TOKEN = """() => {
  const key = Object.keys(sessionStorage).find((name) => name.startsWith("oidc.user:"));
  return key ? JSON.parse(sessionStorage.getItem(key)).access_token : null;
}"""


def canonical_roles(user: FixtureUser) -> dict[str, str | None]:
    """The user's role in each tenant a canonical tenant names (None: a member with no grant)."""
    return {tenant: role for tenant, role in user.roles.items() if IDENTITIES.tenants[tenant].canonical}


def _signed_in(page: Page, frontend_url: str, user: FixtureUser) -> str:
    """Sign ``user`` in from the SPA's Sign In; the access token the SPA holds afterwards."""
    page.goto(f"{frontend_url}/login")
    page.get_by_role("button", name="Sign In").click()
    # Keycloak asks a member of several organizations which one; the pick decides nothing here.
    sign_in(page, TARGET.users[user.key], frontend_url, organization=next(iter(user.roles)))
    token = page.evaluate(_READ_ACCESS_TOKEN)
    assert token, f"the SPA holds no signed-in user after {user.key} signed in"
    return token


def _sidebar(page: Page):
    return page.locator("[data-sidebar=sidebar]")


@pytest.mark.parametrize("key", sorted(IDENTITIES.users))
def test_every_fixture_user_gets_exactly_their_fixture_tenants_and_roles(key: str, page: Page, frontend_url: str, api):
    user = IDENTITIES.users[key]
    expected = canonical_roles(user)
    token = _signed_in(page, frontend_url, user)
    identity = api(token).get(IDENTITY)

    if not expected:
        assert (identity.status, identity.json()) == (403, NOT_LINKED)
        expect(page.locator("header").get_by_text(NO_TENANTS, exact=True)).to_be_visible()
        expect(_sidebar(page).get_by_role("link", name=ADMINISTRATION_PAGE)).to_have_count(0)
        return

    assert identity.status == 200, identity.text()
    body = identity.json()
    tenant_ids = {membership["tenant_name"]: membership["tenant_id"] for membership in body["tenant_memberships"]}
    names = {tenant_id: name for name, tenant_id in tenant_ids.items()}
    assert sorted(tenant_ids) == sorted(expected)
    assert {names[entry["tenant_id"]]: entry["role_name"] for entry in body["roles"]} == {
        tenant: role for tenant, role in expected.items() if role is not None
    }

    for tenant, role in expected.items():
        granted = {
            permission: api(token, {"X-Tenant-ID": tenant_ids[tenant]}).get(route)
            for permission, route in ACCESS_CHECKS.items()
        }
        assert {permission: (answer.status, answer.json()) for permission, answer in granted.items()} == {
            permission: (200, {"tenant_id": tenant_ids[tenant], "permission": permission})
            if role in GRANTED_BY[permission]
            else (403, INSUFFICIENT_PERMISSIONS)
            for permission in ACCESS_CHECKS
        }

    # The header names a user's one tenant; it offers a selector only to a member of several.
    header = page.locator("header")
    switcher = header.get_by_role("combobox")
    expect(switcher).to_have_count(1 if len(expected) > 1 else 0)
    for tenant, role in expected.items():
        if len(expected) > 1:
            if switcher.inner_text() != tenant:
                switcher.click()
                page.get_by_role("option", name=tenant).click()
            expect(switcher).to_have_text(tenant)
        else:
            expect(header.get_by_text(tenant, exact=True)).to_be_visible()
        roles = page.get_by_text("Roles:", exact=True)
        if role is None:
            expect(roles).to_have_count(0)
        else:
            expect(roles.locator("xpath=..")).to_contain_text(role)
        expect(_sidebar(page).get_by_role("link", name=ADMINISTRATION_PAGE)).to_have_count(
            1 if role in ADMINISTRATOR_ROLES else 0
        )
