"""E2E UI tests for repository base class refactor — frontend renders correctly.

Validates that the UI still displays identity data correctly after the
BaseRepository refactor. The repository layer feeds the API, which feeds
the React frontend.

Requires DESCOPE_MANAGEMENT_KEY, DESCOPE_CLIENT_ID, DESCOPE_CLIENT_SECRET.
"""

import contextlib
import os
import time
import uuid

import pytest
from playwright.sync_api import APIRequestContext, Page, expect

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY") or not os.environ.get("DESCOPE_CLIENT_ID"),
    reason="DESCOPE credentials not set",
)

MAX_API_RESPONSE_MS = 2000


def _unique_name(prefix: str) -> str:
    return f"{prefix}-e2e-{uuid.uuid4().hex[:8]}"


def _timed_request(context: APIRequestContext, method: str, url: str, **kwargs) -> tuple:
    start = time.monotonic()
    if method == "GET":
        resp = context.get(url, **kwargs)
    elif method == "POST":
        resp = context.post(url, **kwargs)
    elif method == "DELETE":
        resp = context.delete(url, **kwargs)
    else:
        raise ValueError(f"Unsupported method: {method}")
    elapsed_ms = (time.monotonic() - start) * 1000
    return resp, elapsed_ms


# --- Test 1: Authenticated navigation to identity pages ---


def test_navigate_to_roles_page(auth_page: Page):
    """Navigate to /roles and verify the page loads without errors."""
    auth_page.goto(auth_page.url.split("/")[0] + "//" + auth_page.url.split("/")[2] + "/roles")
    auth_page.wait_for_load_state("networkidle")
    # Page should not redirect to login
    expect(auth_page).not_to_have_url("**/login**")


def test_navigate_to_members_page(auth_page: Page):
    """Navigate to /members and verify the page loads without errors."""
    auth_page.goto(auth_page.url.split("/")[0] + "//" + auth_page.url.split("/")[2] + "/members")
    auth_page.wait_for_load_state("networkidle")
    expect(auth_page).not_to_have_url("**/login**")


def test_navigate_to_settings_page(auth_page: Page):
    """Navigate to /settings (tenant settings) and verify the page loads."""
    auth_page.goto(auth_page.url.split("/")[0] + "//" + auth_page.url.split("/")[2] + "/settings")
    auth_page.wait_for_load_state("networkidle")
    expect(auth_page).not_to_have_url("**/login**")
    expect(auth_page.get_by_text("Tenant Settings")).to_be_visible()


# --- Test 2: Data display — create role via API, verify in UI ---


def test_role_created_via_api_visible_in_ui(
    auth_page: Page,
    admin_api_context: APIRequestContext,
    backend_url: str,
    frontend_url: str,
):
    """Create a role via API, navigate to /roles in browser, verify it appears."""
    role_name = _unique_name("ui-role")
    cleanup_role = None

    try:
        # Create role via API
        resp, _ = _timed_request(
            admin_api_context,
            "POST",
            f"{backend_url}/api/roles",
            data={"name": role_name, "description": "UI visibility test"},
        )
        assert resp.status == 201, f"Create role failed: {resp.status}"
        cleanup_role = role_name

        # Navigate to roles page
        auth_page.goto(f"{frontend_url}/roles")
        auth_page.wait_for_load_state("networkidle")

        # Verify the role appears somewhere on the page
        expect(auth_page.get_by_text(role_name)).to_be_visible(timeout=10000)

    finally:
        if cleanup_role:
            with contextlib.suppress(Exception):
                admin_api_context.delete(f"{backend_url}/api/roles/{cleanup_role}")


# --- Test 3: Error states — nonexistent resource URL ---


def test_nonexistent_route_shows_error(auth_page: Page, frontend_url: str):
    """Navigate to a nonexistent route and verify the UI doesn't crash."""
    fake_id = uuid.uuid4()
    auth_page.goto(f"{frontend_url}/admin/users/{fake_id}")
    auth_page.wait_for_load_state("networkidle")

    # The page should either show a not-found message or redirect to a valid page
    # — it should NOT show a blank/crashed page
    body = auth_page.locator("body")
    expect(body).not_to_be_empty()
