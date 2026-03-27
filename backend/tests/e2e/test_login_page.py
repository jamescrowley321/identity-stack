"""E2E tests for the login page and unauthenticated flows."""

import pytest
from playwright.sync_api import Page, expect


def test_login_page_renders(page: Page, frontend_url: str):
    """Login page shows app title and sign-in button."""
    page.goto(frontend_url + "/login")
    expect(page.get_by_text("Descope SaaS Starter")).to_be_visible()
    expect(page.get_by_role("button", name="Sign In")).to_be_visible()


def test_unauthenticated_redirects_to_login(page: Page, frontend_url: str):
    """Accessing protected route without auth redirects to login."""
    page.goto(frontend_url + "/")
    # Should redirect to /login
    page.wait_for_url("**/login**", timeout=10000)
    expect(page.get_by_role("button", name="Sign In")).to_be_visible()


def test_protected_routes_redirect_to_login(page: Page, frontend_url: str):
    """All protected routes redirect to login when unauthenticated."""
    protected_routes = ["/", "/roles", "/profile", "/settings", "/keys", "/members"]
    for route in protected_routes:
        page.goto(frontend_url + route)
        page.wait_for_url("**/login**", timeout=10000)
        expect(page.get_by_role("button", name="Sign In")).to_be_visible()


def test_login_page_has_no_sidebar(page: Page, frontend_url: str):
    """Login page should not show the app shell sidebar."""
    page.goto(frontend_url + "/login")
    expect(page.get_by_text("Descope SaaS Starter")).to_be_visible()
    # Sidebar nav items should not be present on login page
    expect(page.get_by_text("Dashboard")).not_to_be_visible(timeout=2000)
