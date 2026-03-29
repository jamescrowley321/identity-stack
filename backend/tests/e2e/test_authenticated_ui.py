"""E2E tests for authenticated UI flows.

These tests use OIDC token injection to authenticate the browser session.
Requires DESCOPE_CLIENT_ID, DESCOPE_CLIENT_SECRET, and DESCOPE_MANAGEMENT_KEY.
"""

import os

import pytest
from playwright.sync_api import Page, Request, Response, expect

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY") or not os.environ.get("DESCOPE_CLIENT_ID"),
    reason="DESCOPE credentials not set",
)


def test_authenticated_user_sees_dashboard(auth_page: Page):
    """After login, user sees the dashboard (not /login)."""
    expect(auth_page).not_to_have_url("**/login**")
    expect(auth_page.get_by_text("Welcome")).to_be_visible(timeout=10000)


def test_sidebar_navigation_links(auth_page: Page):
    """Sidebar shows navigation links."""
    sidebar = auth_page.locator("[data-slot='sidebar']")
    expect(sidebar.get_by_text("Dashboard")).to_be_visible()
    expect(sidebar.get_by_text("Profile")).to_be_visible()
    expect(sidebar.get_by_text("Tenant Settings")).to_be_visible()


def test_theme_toggle(auth_page: Page):
    """Theme toggle button is functional."""
    toggle = auth_page.get_by_role("button", name="Toggle theme")
    expect(toggle).to_be_visible()
    toggle.click()
    expect(toggle).to_be_visible()


def test_navigate_to_profile(auth_page: Page):
    """Navigate to profile page via sidebar."""
    auth_page.get_by_role("link", name="Profile").click()
    auth_page.wait_for_url("**/profile**")
    expect(auth_page.get_by_text("User Profile")).to_be_visible()


def test_navigate_to_settings(auth_page: Page):
    """Navigate to tenant settings page via sidebar."""
    auth_page.get_by_role("link", name="Tenant Settings").click()
    auth_page.wait_for_url("**/settings**")
    expect(auth_page.get_by_text("Tenant Settings")).to_be_visible()


def test_dashboard_tabs(auth_page: Page):
    """Dashboard has Overview, Claims tabs."""
    expect(auth_page.get_by_role("tab", name="Overview")).to_be_visible()
    expect(auth_page.get_by_role("tab", name="Claims")).to_be_visible()


def test_dashboard_status_card(auth_page: Page):
    """Dashboard Overview shows status info."""
    auth_page.get_by_role("tab", name="Overview").click()
    expect(auth_page.get_by_text("Status")).to_be_visible()
    expect(auth_page.get_by_text("Backend")).to_be_visible()


def test_dashboard_claims_tab(auth_page: Page):
    """Dashboard Claims tab shows token claim cards."""
    auth_page.get_by_role("tab", name="Claims").click()
    expect(auth_page.get_by_text("ClaimsIdentity")).to_be_visible()
    expect(auth_page.get_by_text("Access Token Claims")).to_be_visible()


def _open_user_menu_and_sign_out(page: Page) -> None:
    """Open the user dropdown menu and click 'Sign out'."""
    # The UserMenu trigger is a ghost icon button inside a DropdownMenu.
    # It contains an Avatar with the user's initials. Find it via the
    # header area and click to open the dropdown.
    header = page.locator("header")
    avatar_button = header.locator("button").filter(has=page.locator("[data-slot='avatar']"))
    avatar_button.click()

    # Wait for the dropdown menu to appear and click "Sign out"
    sign_out = page.get_by_role("menuitem", name="Sign out")
    expect(sign_out).to_be_visible()
    sign_out.click()


def test_logout_navigates_to_login(auth_page: Page):
    """Clicking 'Sign out' navigates to /login without server errors."""
    errors: list[Response] = []
    auth_page.on("response", lambda resp: errors.append(resp) if resp.status >= 500 else None)

    _open_user_menu_and_sign_out(auth_page)

    auth_page.wait_for_url("**/login**", timeout=10000)
    assert "/login" in auth_page.url

    server_errors = [f"{r.status} {r.url}" for r in errors]
    assert not server_errors, f"Server errors during logout: {server_errors}"


def test_logout_no_descope_401(auth_page: Page):
    """Logout must not trigger RP-Initiated Logout (401 from Descope).

    Before the fix, react-oidc-context's signoutRedirect() called Descope's
    /oidc/v1/end_session endpoint with the access-key-based token, which
    Descope rejected with 401. The fix uses removeUser() + navigate() instead.
    """
    network_errors: list[Response] = []
    failed_requests: list[Request] = []

    def _capture_descope_errors(resp: Response) -> None:
        # Capture any 4xx/5xx response to Descope OIDC logout endpoints
        if "descope.com" in resp.url and resp.status >= 400:
            network_errors.append(resp)

    def _capture_descope_failures(req: Request) -> None:
        # Capture connection-level failures (DNS, refused, timeout) to Descope
        if "descope.com" in req.url:
            failed_requests.append(req)

    auth_page.on("response", _capture_descope_errors)
    auth_page.on("requestfailed", _capture_descope_failures)

    _open_user_menu_and_sign_out(auth_page)

    auth_page.wait_for_url("**/login**", timeout=10000)

    descope_errors = [f"{r.status} {r.url}" for r in network_errors]
    assert not descope_errors, f"Descope endpoint errors during logout (RP-Initiated Logout bug): {descope_errors}"

    connection_failures = [f"{r.url} ({r.failure})" for r in failed_requests]
    assert not connection_failures, f"Descope connection failures during logout: {connection_failures}"
