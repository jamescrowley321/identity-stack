"""The SPA opens the tenant of the organization a sign-in named.

A member of two organizations (``IDENTITIES.cases.two_tenants``) signs in from
the SPA's own Sign In, picking each organization in turn when Keycloak asks
which one. ``GET /api/identity`` names each membership's organization at
Keycloak (``external_org_id``), and the SPA's tenant selector starts on the
membership whose organization the token's ``organization`` claim names, not on
the first one ``GET /api/identity`` lists.
"""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_keycloak.browser_login import sign_in
from tests.helpers.identity_fixture import IDENTITIES
from tests.helpers.keycloak import TARGET

IDENTITY = "/api/identity"
TWO_TENANTS = IDENTITIES.cases.two_tenants
# A Keycloak organization's alias is its fixture tenant's key; a linked tenant has its name.
TENANT_KEYS = {tenant.name: key for key, tenant in IDENTITIES.tenants.items()}

# oidc-client-ts keeps the signed-in user in sessionStorage under this prefix.
_READ_ACCESS_TOKEN = """() => {
  const key = Object.keys(sessionStorage).find((name) => name.startsWith("oidc.user:"));
  return key ? JSON.parse(sessionStorage.getItem(key)).access_token : null;
}"""


@pytest.mark.parametrize("alias", sorted(TWO_TENANTS.roles))
def test_sign_in_opens_the_tenant_of_the_organization_picked(alias: str, page: Page, frontend_url: str, api):
    page.goto(f"{frontend_url}/login")
    page.get_by_role("button", name="Sign In").click()
    forms = sign_in(page, TARGET.users[TWO_TENANTS.key], frontend_url, organization=alias)
    assert "organization" in forms, f"Keycloak asked {TWO_TENANTS.key} no organization: {forms}"
    token = page.evaluate(_READ_ACCESS_TOKEN)
    assert token, f"the SPA holds no signed-in user after {TWO_TENANTS.key} signed in"

    identity = api(token).get(IDENTITY)
    assert identity.status == 200, identity.text()
    assert {
        TENANT_KEYS[membership["tenant_name"]]: membership["external_org_id"]
        for membership in identity.json()["tenant_memberships"]
    } == {tenant: tenant for tenant in TWO_TENANTS.roles}

    switcher = page.locator("header").get_by_role("combobox")
    expect(switcher).to_have_text(IDENTITIES.tenants[alias].name)
