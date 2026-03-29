"""E2E test configuration and fixtures."""

import os

import pytest

from tests.e2e.helpers.auth import (
    create_authenticated_context,
    ensure_test_user,
    get_admin_session_token,
    get_oidc_access_token,
)

FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:3000")
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:8000")

_has_mgmt_key = bool(os.environ.get("DESCOPE_MANAGEMENT_KEY"))
_has_client_creds = bool(os.environ.get("DESCOPE_CLIENT_ID") and os.environ.get("DESCOPE_CLIENT_SECRET"))


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


@pytest.fixture(scope="session")
def admin_access_token():
    """OIDC token from a tenant-scoped access key with admin/owner roles.

    Creates a temporary access key with tenant/role associations, then uses it
    in the OIDC client credentials flow. The resulting token passes OIDC
    validation AND contains dct/tenants claims needed by require_role().
    """
    if not _has_mgmt_key:
        pytest.skip("DESCOPE_MANAGEMENT_KEY not set")
    return get_admin_session_token()


@pytest.fixture
def admin_api_context(playwright, admin_access_token):
    """API request context with admin-level session token (has tenant/role claims)."""
    context = playwright.request.new_context(
        base_url=BACKEND_URL,
        extra_http_headers={"Authorization": f"Bearer {admin_access_token}"},
    )
    yield context
    context.dispose()


@pytest.fixture(scope="session")
def test_user_id(_ensure_test_user) -> str:
    """The Descope login ID (email) of the E2E test user.

    Uses loginIds[0] (the user's email) because the Descope Management API's
    /v1/mgmt/user/update/role/add expects loginId, not the internal userId.
    """
    login_ids = _ensure_test_user.get("loginIds", [])
    if login_ids:
        return login_ids[0]
    # Fallback to userId if loginIds not present
    user_id = _ensure_test_user.get("userId", "")
    if not user_id:
        pytest.skip("Could not determine test user ID")
    return user_id


@pytest.fixture(scope="session")
def test_tenant_id() -> str:
    """The Descope tenant ID for E2E tests."""
    tid = os.environ.get("E2E_TEST_TENANT_ID", "")
    if not tid:
        pytest.skip("E2E_TEST_TENANT_ID not set")
    return tid


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
