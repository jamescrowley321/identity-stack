"""The SPA shows exactly the pages the deployment serves the caller (#678, #683).

``GET /api/capabilities`` reports the sync adapter's capabilities and the
relationship store as they serve the caller, and whether the caller is a principal
of the sync provider, whose member and role routes serve only its principals. A
page the caller is not served is neither in the sidebar nor reachable by its
address, and its routes refuse the caller. Every page the SPA does show is visited,
and any 403 from the API on it fails the run.
"""

import os
import re
from urllib.parse import urlsplit

from playwright.sync_api import Page, Response, expect

from app.dependencies.sync_provider import NOT_A_SYNC_PROVIDER_PRINCIPAL
from app.services.adapters.base import Capability
from app.services.relationship_store import RelationshipStoreKind
from tests.e2e.not_applicable import DESCOPE, KEYCLOAK, ZITADEL, selected_provider

# What each run's principals are served. A sync adapter's capabilities, and its member
# and role routes, serve its own provider's principals only, and so does Descope's FGA
# store: the Keycloak run syncs with Keycloak (custom attributes, and machine
# credentials, which are canonical and serve every principal, #680), while the Zitadel
# run's backend syncs with Descope (SYNC_PROVIDER stays descope, .github/workflows/ci.yml;
# #709 runs it with zitadel) and serves its Zitadel principals none of Descope's. Both
# canonical runs apply infra/openfga, whose RELATIONSHIP_STORE=openfga serves their
# principals FGA and documents by canonical id (#682).
EXPECTED = {
    DESCOPE: {
        "capabilities": sorted([Capability.CUSTOM_ATTRIBUTES, Capability.MACHINE_CREDENTIALS]),
        "relationship_store": RelationshipStoreKind.DESCOPE,
        "sync_provider_principal": True,
    },
    KEYCLOAK: {
        "capabilities": sorted([Capability.CUSTOM_ATTRIBUTES, Capability.MACHINE_CREDENTIALS]),
        "relationship_store": RelationshipStoreKind.OPENFGA,
        "sync_provider_principal": True,
    },
    ZITADEL: {
        "capabilities": [],
        "relationship_store": RelationshipStoreKind.OPENFGA,
        "sync_provider_principal": False,
    },
}

# The sidebar links shown whatever the deployment serves, to an admin and to anyone.
ALWAYS_FOR_ADMIN = {"Providers", "Sync Dashboard", "Events", "Provisional Users"}
ALWAYS = {"Dashboard", "Profile"}

CAPABILITY_MISSING = (404, "/errors/capability-missing")
NOT_THE_SYNC_PROVIDER = (403, NOT_A_SYNC_PROVIDER_PRINCIPAL.format(provider="Descope"))


def _machine_credentials(served: dict) -> bool:
    return Capability.MACHINE_CREDENTIALS in served["capabilities"]


def _custom_attributes(served: dict) -> bool:
    return Capability.CUSTOM_ATTRIBUTES in served["capabilities"]


def _relationship_store(served: dict) -> bool:
    return served["relationship_store"] != RelationshipStoreKind.NONE


def _sync_provider(served: dict) -> bool:
    return served["sync_provider_principal"]


# Each gated page: its sidebar label -> (address, one of its routes, admins only, given by,
# what that route answers a caller it does not serve). The Zitadel run's sync provider
# is Descope, the only one a run can refuse with the fence.
GATED_PAGES = {
    "Access Keys": ("/keys", "/api/keys", True, _machine_credentials, CAPABILITY_MISSING),
    "FGA": ("/fga", "/api/fga/schema", True, _relationship_store, CAPABILITY_MISSING),
    "Tenant Settings": ("/settings", "/api/tenants/current/settings", False, _custom_attributes, CAPABILITY_MISSING),
    "Members": ("/members", "/api/members", True, _sync_provider, NOT_THE_SYNC_PROVIDER),
    "Roles": ("/roles", "/api/roles", True, _sync_provider, NOT_THE_SYNC_PROVIDER),
}

# Whether the SPA shows each run's admin fixture as an admin of its tenant. The
# Descope run's admin is a test user with no canonical identity, which the SPA shows no admin role:
# that run checks the pages a member sees.
SPA_ADMIN = {DESCOPE: False, KEYCLOAK: True, ZITADEL: True}


def _served(admin_api_context) -> dict:
    response = admin_api_context.get("/api/capabilities")
    assert response.status == 200, response.text()
    return response.json()


def _sidebar(page: Page):
    return page.locator("[data-sidebar=sidebar]")


def _shown_links(page: Page) -> set[str]:
    sidebar = _sidebar(page)
    expect(sidebar.get_by_role("link", name="Dashboard", exact=True)).to_be_visible()
    return {text.strip() for text in sidebar.get_by_role("link").all_inner_texts()}


def test_the_backend_reports_what_serves_the_runs_principals(admin_api_context):
    assert _served(admin_api_context) == EXPECTED[selected_provider(os.environ)]


def test_the_sidebar_shows_exactly_the_pages_the_capabilities_give(admin_page: Page, admin_api_context):
    served = _served(admin_api_context)
    shown = _shown_links(admin_page)
    admin = SPA_ADMIN[selected_provider(os.environ)]
    assert ("Providers" in shown) == admin, f"the SPA shows the admin fixture as admin: {'Providers' in shown}"
    expected = (
        ALWAYS
        | (ALWAYS_FOR_ADMIN if admin else set())
        | {
            label
            for label, (_, _, admins_only, given, _) in GATED_PAGES.items()
            if given(served) and (admin or not admins_only)
        }
    )

    assert shown == expected


def test_a_page_not_served_opens_the_dashboard(admin_page: Page, frontend_url: str, admin_api_context):
    served = _served(admin_api_context)

    for address, _, _, given, _ in GATED_PAGES.values():
        if not given(served):
            admin_page.goto(f"{frontend_url}{address}")
            expect(admin_page).to_have_url(re.compile(rf"^{re.escape(frontend_url)}/?$"))


def test_the_routes_of_a_page_not_served_refuse_the_caller(admin_api_context):
    served = _served(admin_api_context)
    missing = {route: refusal for _, route, _, given, refusal in GATED_PAGES.values() if not given(served)}

    answers = {}
    for route in missing:
        response = admin_api_context.get(route)
        body = response.json()
        answers[route] = (response.status, body["type"] if response.status == 404 else body["detail"])

    assert answers == missing


def test_no_page_the_sidebar_shows_answers_403(admin_page: Page, frontend_url: str):
    """Visit every page the sidebar shows the admin and fail on any 403 from the API."""
    links = _shown_links(admin_page)
    addresses = {
        label: _sidebar(admin_page).get_by_role("link", name=label, exact=True).get_attribute("href") for label in links
    }
    refused: list[str] = []
    visiting = [""]

    def record(response: Response) -> None:
        path = urlsplit(response.url).path
        if response.status == 403 and path.startswith("/api/"):
            refused.append(f"{visiting[0]}: {response.request.method} {path}")

    admin_page.on("response", record)
    for label, address in sorted(addresses.items()):
        visiting[0] = label
        admin_page.goto(f"{frontend_url}{address}")
        admin_page.wait_for_load_state("networkidle")
    admin_page.remove_listener("response", record)

    assert refused == []
