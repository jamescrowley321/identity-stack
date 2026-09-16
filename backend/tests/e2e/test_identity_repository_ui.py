"""E2E UI tests for repository base class refactor — frontend renders correctly.

Validates that the UI still displays identity data correctly after the
BaseRepository refactor. The repository layer feeds the API, which feeds
the React frontend.

Requires DESCOPE_MANAGEMENT_KEY, DESCOPE_CLIENT_ID, DESCOPE_CLIENT_SECRET.
"""

import os
import re
import uuid

import pytest
from playwright.sync_api import APIRequestContext, Page, expect

from tests.e2e.helpers.api import unique_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY") or not os.environ.get("DESCOPE_CLIENT_ID"),
    reason="DESCOPE credentials not set",
)


# --- Test 1: Authenticated navigation to identity pages ---


def test_navigate_to_roles_page(auth_page: Page, frontend_url: str):
    """Navigate to /roles and verify the page loads without errors."""
    auth_page.goto(f"{frontend_url}/roles")
    auth_page.wait_for_load_state("networkidle")
    # Anchor on what the page renders before asserting where we are not. A bare
    # not_to_* resolves immediately, so on its own it passes against a page that
    # has not finished loading — for the wrong reason.
    expect(auth_page.get_by_role("heading", name="Role Management", level=1)).to_be_visible()
    # A regex, not a glob: not_to_have_url takes an exact string or a Pattern and
    # does not translate glob syntax, so "**/login**" asserted only that the URL
    # is not that literal 11-character string, which can never fail.
    expect(auth_page).not_to_have_url(re.compile(r"/login"))


def test_navigate_to_members_page(auth_page: Page, frontend_url: str):
    """Navigate to /members and verify the page loads without errors."""
    auth_page.goto(f"{frontend_url}/members")
    auth_page.wait_for_load_state("networkidle")
    expect(auth_page.get_by_role("heading", name="Members", level=1)).to_be_visible()
    expect(auth_page).not_to_have_url(re.compile(r"/login"))


def test_navigate_to_settings_page(admin_page: Page, frontend_url: str):
    """Navigate to /settings (tenant settings) and verify the page loads.

    Browses as an admin: the settings fetch 403s for an identity with no tenant
    claims and TenantSettings then renders <Unauthorized/>, which carries no
    page heading. The old assertion — a bare "Tenant Settings" text match —
    passed against the sidebar link and the header breadcrumb, so it never
    noticed the page itself had not rendered.
    """
    admin_page.goto(f"{frontend_url}/settings")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page).not_to_have_url(re.compile(r"/login"))
    # Scoped to the page's own <h1>: the sidebar and breadcrumb carry the same
    # words, so an unscoped match is both a strict-mode violation and no proof.
    expect(admin_page.get_by_role("heading", name="Tenant Settings", level=1)).to_be_visible()


# --- Test 2: Data display — create role via API, verify in UI ---


def test_role_created_via_api_is_invisible_to_an_access_key_session(
    admin_page: Page,
    admin_api_context: APIRequestContext,
    backend_url: str,
    frontend_url: str,
):
    """A role created through the admin API stays invisible to THIS browser session.

    This is a statement about the fixture's identity, not about where useRBAC
    reads roles from. ``get_admin_session_token`` mints a tenant-scoped access
    key, exchanges it for a session JWT and deletes the key, so the JWT's ``sub``
    is the access key id (``K...``) rather than a Descope user id (``U...``).

    ``GET /api/identity`` answers from the canonical model — provider row, then
    an IdP link on ``external_sub`` — and IdP links only ever come from Descope
    *users* (``scripts/seed_descope.import_idp_links``). An access key is not a
    user and cannot have one, and Descope is not in ``JIT_ENABLED_PROVIDERS``
    (default ``{"ory"}``), so nothing provisions one on the fly. The endpoint
    404s for this subject however the database is seeded. IdentityContext fails
    closed on that, useTenants stays empty, ``useRBAC().isAdmin`` is false, and
    the admin-gated ``Role Definitions`` card — the only place a role nobody is
    assigned would show up — never renders.

    An earlier version promised this test would start failing once #392 sourced
    useRBAC from ``GET /api/identity``. It could not: #392 changed which payload
    the client reads, and this session has no canonical identity to read from.
    Verified live in #455.

    So the 404 is asserted outright instead of being left implicit. If an
    access-key session ever does resolve to a canonical identity, this test fails
    on that assertion, and that is the point to rewrite it into a visibility
    assertion. Browser-level coverage of #392 needs a session whose subject is a
    canonical user — tracked in #458.
    """
    # The mechanism, asserted before anything that depends on it. Matched on the
    # detail too: a bare 404 would also be what a renamed or unmounted route
    # returns, and this must fail if the identity resolves — not if it moves.
    identity_resp = admin_api_context.get(f"{backend_url}/api/identity")
    assert identity_resp.status == 404, (
        "This session is expected to have no canonical identity, but "
        f"GET /api/identity returned {identity_resp.status}. If access-key "
        "sessions now resolve, rewrite this test to assert the role IS visible."
    )
    assert identity_resp.json().get("detail") == "Identity not found", (
        f"Expected the endpoint's own 404, got body {identity_resp.text()!r}"
    )

    role_name = unique_name("ui-role")
    cleanup_role = None

    try:
        resp = admin_api_context.post(
            f"{backend_url}/api/roles",
            data={"name": role_name, "description": "UI visibility test"},
        )
        assert resp.status == 201, f"Create role failed: {resp.status}"
        cleanup_role = role_name

        admin_page.goto(f"{frontend_url}/roles")
        admin_page.wait_for_load_state("networkidle")
        # A regex, not a glob. `expect(page).not_to_have_url` takes an exact
        # string or a Pattern — it does not translate `**/login**`, so the glob
        # form asserts "the URL is not literally that 11-character string" and
        # can never fail.
        expect(admin_page).not_to_have_url(re.compile(r"/login"))
        # Anchor on what the page must render before asserting any absence, so a
        # negative cannot resolve against a page that has not finished loading.
        expect(admin_page.get_by_role("heading", name="Role Management", level=1)).to_be_visible()
        expect(admin_page.locator("[data-slot='card-title']").filter(has_text="Your Roles")).to_have_count(1)

        # The card is absent, not merely hidden — it is gated on isAdmin and is
        # never mounted. Asserting its count pins the cause; asserting only the
        # role's absence would also pass if the card rendered empty.
        expect(admin_page.locator("[data-slot='card-title']").filter(has_text="Role Definitions")).to_have_count(0)
        expect(admin_page.get_by_text(role_name)).to_have_count(0)

    finally:
        if cleanup_role:
            # Report a failed delete rather than swallowing it. The session sweep
            # collects the leftover, but silence here made the leak invisible.
            try:
                resp = admin_api_context.delete(f"{backend_url}/api/roles/{cleanup_role}")
                if resp.status not in (200, 204):
                    print(f"[E2E] LEAK: role {cleanup_role} not deleted: HTTP {resp.status}")
            except Exception as exc:  # noqa: BLE001 - never mask the test's own failure
                print(f"[E2E] LEAK: error deleting role {cleanup_role}: {exc!r}")


# --- Test 3: Error states — nonexistent resource URL ---


def test_nonexistent_route_redirects_to_the_app_root(auth_page: Page, frontend_url: str):
    """An unknown route redirects into the app, and the app is still mounted.

    `expect(body).not_to_be_empty()` could not detect the thing it was written to
    catch. React mounts into `<div id="root">`, so a total unmount still leaves
    body with that child — non-empty — and the assertion passed straight through
    the SPA crash fixed in 38e6ab3.

    Assert the two things that actually distinguish "recovered" from "crashed":
    the router honoured `<Route path="*" element={<Navigate to="/" replace />}>`,
    and the app shell rendered afterwards.
    """
    fake_id = uuid.uuid4()
    auth_page.goto(f"{frontend_url}/admin/users/{fake_id}")
    auth_page.wait_for_load_state("networkidle")

    # The shell, not the body: proves React is still mounted and rendering.
    expect(auth_page.locator("[data-slot='sidebar']")).to_be_visible(timeout=10000)
    # Redirected off the unknown path, and not bounced to login.
    expect(auth_page).not_to_have_url(re.compile(str(fake_id)))
    expect(auth_page).not_to_have_url(re.compile(r"/login"))
