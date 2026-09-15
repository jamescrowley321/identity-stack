"""E2E UI tests for repository base class refactor — frontend renders correctly.

Validates that the UI still displays identity data correctly after the
BaseRepository refactor. The repository layer feeds the API, which feeds
the React frontend.

Requires DESCOPE_MANAGEMENT_KEY, DESCOPE_CLIENT_ID, DESCOPE_CLIENT_SECRET.
"""

import contextlib
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
    expect(auth_page).not_to_have_url("**/login**")


def test_navigate_to_members_page(auth_page: Page, frontend_url: str):
    """Navigate to /members and verify the page loads without errors."""
    auth_page.goto(f"{frontend_url}/members")
    auth_page.wait_for_load_state("networkidle")
    expect(auth_page).not_to_have_url("**/login**")


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
    expect(admin_page).not_to_have_url("**/login**")
    # Scoped to the page's own <h1>: the sidebar and breadcrumb carry the same
    # words, so an unscoped match is both a strict-mode violation and no proof.
    expect(admin_page.get_by_role("heading", name="Tenant Settings", level=1)).to_be_visible()


# --- Test 2: Data display — create role via API, verify in UI ---


def test_role_created_via_api_is_not_yet_visible_in_ui(
    admin_page: Page,
    admin_api_context: APIRequestContext,
    backend_url: str,
    frontend_url: str,
):
    """Create a role via API, navigate to /roles, and pin what the UI does today.

    This used to be ``xfail(strict=True)`` on the documented useRBAC gap. An
    xfail covers the whole call phase, so it also swallowed the precondition:
    verified locally that a fixture setup error and a failing
    ``assert resp.status == 201`` BOTH report XFAIL with exit code 0. A 500 from
    ``POST /api/roles``, a broken admin token, or a frontend that never came up
    would all have shipped green here.

    So the gap is asserted instead of marked. The role must be created (a real
    assertion), and the table must still not show it — which is the behavior
    #392 changes. When sourcing useRBAC from ``GET /api/identity`` lands, this
    test fails and is rewritten to assert visibility.
    """
    role_name = unique_name("ui-role")
    cleanup_role = None

    try:
        resp = admin_api_context.post(
            f"{backend_url}/api/roles",
            data={"name": role_name, "description": "UI visibility test"},
        )
        assert resp.status == 201, f"Create role failed: {resp.status}"
        cleanup_role = role_name

        # The roles table is gated on useRBAC().isAdmin, which reads roles out of
        # the JWT's `tenants` claim. For this identity that claim carries the
        # tenant but no roles, so the page renders "Roles: None" beside
        # "Server-confirmed: owner, admin" — the client cannot see what the server
        # can. Sourcing useRBAC from the canonical GET /api/identity is #392; this
        # test asserts the behaviour that lands with it.
        admin_page.goto(f"{frontend_url}/roles")
        admin_page.wait_for_load_state("networkidle")
        # A regex, not a glob. `expect(page).not_to_have_url` takes an exact
        # string or a Pattern — it does not translate `**/login**`, so the glob
        # form asserts "the URL is not literally that 11-character string" and
        # can never fail. (Same form still sits in the assertions this test does
        # not own.)
        expect(admin_page).not_to_have_url(re.compile(r"/login"))
        # Anchor on something the page must render BEFORE asserting the absence
        # of the role, so the negative cannot resolve against a page that has
        # not finished loading — which would pass whether or not #392 landed.
        expect(admin_page.get_by_role("heading", name="Role Management", level=1)).to_be_visible()
        expect(admin_page.get_by_text(role_name)).not_to_be_visible()

    finally:
        if cleanup_role:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{cleanup_role}")


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
