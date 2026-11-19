"""The SPA shows exactly the pages the deployment's capabilities give (#678).

``GET /api/capabilities`` reports the sync adapter's capabilities and the
relationship store's kind. A page whose capability is missing is neither in the
sidebar nor reachable by its address, and its routes answer 404.
"""

import os
import re

from playwright.sync_api import Page, expect

from tests.e2e.not_applicable import DESCOPE, KEYCLOAK, ZITADEL, selected_provider

DESCOPE_CAPABILITIES = {"capabilities": ["custom_attributes", "machine_credentials"], "relationship_store": "descope"}

# What each run's backend reports. The Zitadel run's backend syncs with Descope
# (SYNC_PROVIDER stays descope, .github/workflows/ci.yml), so it reports Descope's.
EXPECTED = {
    DESCOPE: DESCOPE_CAPABILITIES,
    KEYCLOAK: {"capabilities": [], "relationship_store": "none"},
    ZITADEL: DESCOPE_CAPABILITIES,
}

# The sidebar links an admin sees whatever the capabilities.
ALWAYS_FOR_ADMIN = {
    "Dashboard",
    "Members",
    "Roles",
    "Providers",
    "Sync Dashboard",
    "Events",
    "Provisional Users",
    "Profile",
}

# Each gated page: its sidebar label, its address, one of its routes, and whether
# the capabilities give it.
GATED_PAGES = {
    "Access Keys": ("/keys", "/api/keys", lambda c: "machine_credentials" in c["capabilities"]),
    "FGA": ("/fga", "/api/fga/schema", lambda c: c["relationship_store"] != "none"),
    "Tenant Settings": (
        "/settings",
        "/api/tenants/current/settings",
        lambda c: "custom_attributes" in c["capabilities"],
    ),
}


def _capabilities(admin_api_context) -> dict:
    response = admin_api_context.get("/api/capabilities")
    assert response.status == 200, response.text()
    return response.json()


def test_the_backend_reports_the_runs_capabilities(admin_api_context):
    assert _capabilities(admin_api_context) == EXPECTED[selected_provider(os.environ)]


def test_the_sidebar_shows_exactly_the_pages_the_capabilities_give(admin_page: Page, admin_api_context):
    capabilities = _capabilities(admin_api_context)
    expected = ALWAYS_FOR_ADMIN | {label for label, (_, _, given) in GATED_PAGES.items() if given(capabilities)}
    sidebar = admin_page.locator("[data-slot='sidebar']")
    expect(sidebar.get_by_role("link", name="Dashboard")).to_be_visible()

    assert {text.strip() for text in sidebar.get_by_role("link").all_inner_texts()} == expected


def test_a_page_without_its_capability_opens_the_dashboard(admin_page: Page, frontend_url: str, admin_api_context):
    capabilities = _capabilities(admin_api_context)
    missing = [address for address, _, given in GATED_PAGES.values() if not given(capabilities)]

    for address in missing:
        admin_page.goto(f"{frontend_url}{address}")
        expect(admin_page).to_have_url(re.compile(rf"^{re.escape(frontend_url)}/?$"))


def test_a_missing_capabilitys_routes_answer_404(admin_api_context):
    capabilities = _capabilities(admin_api_context)
    answers = {
        route: (response.status, response.json()["type"])
        for _, route, given in GATED_PAGES.values()
        if not given(capabilities)
        for response in [admin_api_context.get(route)]
    }

    assert answers == {
        route: (404, "/errors/capability-missing")
        for _, route, given in GATED_PAGES.values()
        if not given(capabilities)
    }
