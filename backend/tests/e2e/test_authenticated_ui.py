"""E2E tests for authenticated UI flows.

These tests use OIDC token injection to authenticate the browser session.
Requires DESCOPE_CLIENT_ID, DESCOPE_CLIENT_SECRET, and DESCOPE_MANAGEMENT_KEY.
"""

import os

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY")
    or not os.environ.get("DESCOPE_CLIENT_ID"),
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
