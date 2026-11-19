"""The SPA shows exactly the pages the deployment's capabilities give the caller (#678).

``GET /api/capabilities`` reports the sync adapter's capabilities and the
relationship store as they serve the caller. A page whose capability is missing is
neither in the sidebar nor reachable by its address, its routes answer 404, and no
page the SPA does show answers 403 to an admin of the tenant.
"""

import os
import re
from urllib.parse import urlsplit

from playwright.sync_api import Page, Response, expect

from app.services.adapters.base import Capability
from app.services.relationship_store import RelationshipStoreKind
from tests.e2e.not_applicable import DESCOPE, KEYCLOAK, ZITADEL, selected_provider

NOTHING = {"capabilities": [], "relationship_store": RelationshipStoreKind.NONE}

# What each run's principals are served. Descope's capabilities and FGA store are
# keyed on Descope ids, so they serve Descope principals only: the Zitadel run's
# backend syncs with Descope (SYNC_PROVIDER stays descope, .github/workflows/ci.yml),
# but serves its Zitadel principals none of them.
EXPECTED = {
    DESCOPE: {
        "capabilities": sorted([Capability.CUSTOM_ATTRIBUTES, Capability.MACHINE_CREDENTIALS]),
        "relationship_store": RelationshipStoreKind.DESCOPE,
    },
    KEYCLOAK: NOTHING,
    ZITADEL: NOTHING,
}

# The sidebar links shown whatever the capabilities, to an admin and to anyone.
ALWAYS_FOR_ADMIN = {"Members", "Roles", "Providers", "Sync Dashboard", "Events", "Provisional Users"}
ALWAYS = {"Dashboard", "Profile"}


def _machine_credentials(served: dict) -> bool:
    return Capability.MACHINE_CREDENTIALS in served["capabilities"]


def _custom_attributes(served: dict) -> bool:
    return Capability.CUSTOM_ATTRIBUTES in served["capabilities"]


def _relationship_store(served: dict) -> bool:
    return served["relationship_store"] != RelationshipStoreKind.NONE


# Each gated page: its sidebar label -> (address, one of its routes, admins only, given by).
GATED_PAGES = {
    "Access Keys": ("/keys", "/api/keys", True, _machine_credentials),
    "FGA": ("/fga", "/api/fga/schema", True, _relationship_store),
    "Tenant Settings": ("/settings", "/api/tenants/current/settings", False, _custom_attributes),
}

# The pages this test does not visit, and the API calls whose 403 it does not count,
# each with the story that moves them onto the canonical model (epic #672).
NOT_YET_CANONICAL_PAGES = {
    "Members": "GET /api/members serves Descope principals only until #677",
    "Roles": "/api/roles and /api/permissions serve Descope principals only until #676",
}
NOT_YET_CANONICAL_CALLS = {
    re.compile(r"^/api/tenants$"): "the Dashboard's tenant list reads Descope's tenants claim until #673",
    re.compile(r"^/api/tenants/[^/]+/resources$"): "the Dashboard's resources read Descope's claim until #673",
}


def _served(admin_api_context) -> dict:
    response = admin_api_context.get("/api/capabilities")
    assert response.status == 200, response.text()
    return response.json()


def _sidebar(page: Page):
    return page.locator("[data-sidebar=sidebar]")


def _shown_links(page: Page) -> set[str]:
    sidebar = _sidebar(page)
    expect(sidebar.get_by_role("link", name="Dashboard")).to_be_visible()
    return {text.strip() for text in sidebar.get_by_role("link").all_inner_texts()}


def test_the_backend_reports_what_serves_the_runs_principals(admin_api_context):
    assert _served(admin_api_context) == EXPECTED[selected_provider(os.environ)]


def test_the_sidebar_shows_exactly_the_pages_the_capabilities_give(admin_page: Page, admin_api_context):
    served = _served(admin_api_context)
    shown = _shown_links(admin_page)
    # Whether the SPA treats this session as an admin is the RBAC's answer, not the
    # capabilities': the test holds the gated pages to both.
    admin = "Members" in shown
    expected = (
        ALWAYS
        | (ALWAYS_FOR_ADMIN if admin else set())
        | {
            label
            for label, (_, _, admins_only, given) in GATED_PAGES.items()
            if given(served) and (admin or not admins_only)
        }
    )

    assert shown == expected


def test_a_page_without_its_capability_opens_the_dashboard(admin_page: Page, frontend_url: str, admin_api_context):
    served = _served(admin_api_context)

    for address, _, _, given in GATED_PAGES.values():
        if not given(served):
            admin_page.goto(f"{frontend_url}{address}")
            expect(admin_page).to_have_url(re.compile(rf"^{re.escape(frontend_url)}/?$"))


def test_a_missing_capabilitys_routes_answer_404(admin_api_context):
    served = _served(admin_api_context)
    missing = [route for _, route, _, given in GATED_PAGES.values() if not given(served)]

    answers = {route: (r.status, r.json()["type"]) for route in missing for r in [admin_api_context.get(route)]}

    assert answers == {route: (404, "/errors/capability-missing") for route in missing}


def test_no_page_the_sidebar_shows_answers_403(admin_page: Page, frontend_url: str):
    """Visit every page the sidebar shows the admin and fail on any 403 from the API."""
    links = _shown_links(admin_page) - set(NOT_YET_CANONICAL_PAGES)
    addresses = {
        label: _sidebar(admin_page).get_by_role("link", name=label, exact=True).get_attribute("href") for label in links
    }
    refused: list[str] = []
    visiting = [""]

    def record(response: Response) -> None:
        path = urlsplit(response.url).path
        if (
            response.status == 403
            and path.startswith("/api/")
            and not any(call.match(path) for call in NOT_YET_CANONICAL_CALLS)
        ):
            refused.append(f"{visiting[0]}: {response.request.method} {path}")

    admin_page.on("response", record)
    for label, address in sorted(addresses.items()):
        visiting[0] = label
        admin_page.goto(f"{frontend_url}{address}")
        admin_page.wait_for_load_state("networkidle")
    admin_page.remove_listener("response", record)

    assert refused == []
