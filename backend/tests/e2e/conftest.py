"""E2E test configuration and fixtures."""

import os

import pytest

from tests.e2e.helpers.auth import (
    create_authenticated_context,
    ensure_test_user,
    get_oidc_access_token,
)

FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:3000")
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:8000")

_has_mgmt_key = bool(os.environ.get("DESCOPE_MANAGEMENT_KEY"))
_has_client_creds = bool(
    os.environ.get("DESCOPE_CLIENT_ID") and os.environ.get("DESCOPE_CLIENT_SECRET")
)


@pytest.fixture(scope="session")
def frontend_url():
    return FRONTEND_URL


@pytest.fixture(scope="session")
def backend_url():
    return BACKEND_URL


@pytest.fixture(scope="session")
def browser_context_args():
    return {"viewport": {"width": 1280, "height": 720}, "ignore_https_errors": True}


@pytest.fixture
def api_context(playwright):
    """Unauthenticated API request context."""
    context = playwright.request.new_context(base_url=BACKEND_URL)
    yield context
    context.dispose()


# --- Authenticated fixtures ---


@pytest.fixture(scope="session")
def _ensure_test_user():
    if not _has_mgmt_key:
        pytest.skip("DESCOPE_MANAGEMENT_KEY not set")
    return ensure_test_user()


@pytest.fixture(scope="session")
def auth_access_token():
    """OIDC-compatible access token via client credentials flow."""
    if not _has_client_creds:
        pytest.skip("DESCOPE_CLIENT_ID/DESCOPE_CLIENT_SECRET not set")
    return get_oidc_access_token()


@pytest.fixture
def auth_api_context(playwright, auth_access_token):
    """API request context with valid OIDC auth token."""
    context = playwright.request.new_context(
        base_url=BACKEND_URL,
        extra_http_headers={"Authorization": f"Bearer {auth_access_token}"},
    )
    yield context
    context.dispose()


@pytest.fixture
def auth_page(browser, _ensure_test_user, auth_access_token, frontend_url):
    """Browser page with OIDC tokens injected for authenticated testing.

    Uses client credentials access token injected into sessionStorage
    via add_init_script, so react-oidc-context treats the session as
    authenticated on first render.
    """
    context = create_authenticated_context(browser, frontend_url, auth_access_token)
    page = context.new_page()
    page.goto(frontend_url + "/")
    page.wait_for_load_state("networkidle")
    yield page
    context.close()
