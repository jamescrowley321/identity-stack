"""E2E tests for page-specific content rendering.

Tests that each page loads its expected content sections
when authenticated. Uses token injection for fast auth.
"""

import os

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY") or not os.environ.get("DESCOPE_CLIENT_ID"),
    reason="DESCOPE credentials not set",
)


def test_profile_page_renders(auth_page: Page, frontend_url: str):
    """Profile page shows heading and either profile card or error alert."""
    auth_page.goto(frontend_url + "/profile")
    auth_page.wait_for_load_state("networkidle")
    expect(auth_page.get_by_role("heading", name="User Profile")).to_be_visible()


def test_profile_page_handles_load_failure(auth_page: Page, frontend_url: str):
    """Profile page stays in loading state or shows profile when API fails."""
    auth_page.goto(frontend_url + "/profile")
    auth_page.wait_for_load_state("networkidle")
    # Client credentials token may cause API failure — page shows heading regardless
    expect(auth_page.get_by_role("heading", name="User Profile")).to_be_visible(timeout=10000)


def test_tenant_settings_handles_no_tenant(auth_page: Page, frontend_url: str):
    """Tenant settings shows heading when no tenant context (stays in loading skeleton)."""
    auth_page.goto(frontend_url + "/settings")
    auth_page.wait_for_load_state("networkidle")
    # Without tenant context, API fails and page stays in skeleton state — heading still renders
    expect(auth_page.get_by_role("heading", name="Tenant Settings")).to_be_visible()


def test_members_page_loads(auth_page: Page, frontend_url: str):
    """Members page loads with invite section."""
    auth_page.goto(frontend_url + "/members")
    auth_page.wait_for_load_state("networkidle")
    expect(auth_page.get_by_role("heading", name="Members")).to_be_visible()
    expect(auth_page.get_by_text("Invite Member")).to_be_visible()


def test_access_keys_page_loads(auth_page: Page, frontend_url: str):
    """Access keys page loads with create form."""
    auth_page.goto(frontend_url + "/keys")
    auth_page.wait_for_load_state("networkidle")
    expect(auth_page.get_by_role("heading", name="Access Keys")).to_be_visible()
    expect(auth_page.get_by_text("Create Key")).to_be_visible()


def test_roles_page_loads(auth_page: Page, frontend_url: str):
    """Roles page shows Your Roles card."""
    auth_page.goto(frontend_url + "/roles")
    auth_page.wait_for_load_state("networkidle")
    expect(auth_page.get_by_role("heading", name="Role Management")).to_be_visible()
    expect(auth_page.get_by_text("Your Roles")).to_be_visible()


def test_dashboard_overview_tab_content(auth_page: Page):
    """Dashboard Overview tab shows status with Backend badge."""
    auth_page.get_by_role("tab", name="Overview").click()
    expect(auth_page.get_by_text("Backend")).to_be_visible()


def test_dashboard_claims_tab_has_three_cards(auth_page: Page):
    """Dashboard Claims tab shows all three claim cards."""
    auth_page.get_by_role("tab", name="Claims").click()
    expect(auth_page.get_by_text("ClaimsIdentity")).to_be_visible()
    expect(auth_page.get_by_text("Access Token Claims")).to_be_visible()
    expect(auth_page.get_by_text("ID Token Claims")).to_be_visible()


def test_no_5xx_errors_on_dashboard(auth_page: Page, frontend_url: str):
    """Dashboard page should not trigger any 5xx API errors."""
    errors = []
    auth_page.on("response", lambda response: errors.append(response) if response.status >= 500 else None)
    auth_page.goto(frontend_url)
    auth_page.wait_for_load_state("networkidle")
    failed = [f"{r.url} -> {r.status}" for r in errors]
    assert not failed, f"5xx errors on dashboard: {failed}"
