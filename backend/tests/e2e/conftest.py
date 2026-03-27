"""E2E test configuration and fixtures.

Tests run against a live frontend (localhost:3000) + backend (localhost:8000).
Start both servers before running: `make dev-backend` and `make dev-frontend`.

Authentication uses client credentials (access key) flow for OIDC-compatible tokens.
"""

import os

import pytest

from tests.e2e.helpers.auth import get_access_token_via_client_credentials

FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:3000")
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:8000")


@pytest.fixture(scope="session")
def frontend_url():
    return FRONTEND_URL


@pytest.fixture(scope="session")
def backend_url():
    return BACKEND_URL


@pytest.fixture(scope="session")
def browser_context_args():
    """Configure browser context for all tests."""
    return {
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
    }


@pytest.fixture
def api_context(playwright):
    """Create an API request context for direct API testing (unauthenticated)."""
    context = playwright.request.new_context(base_url=BACKEND_URL)
    yield context
    context.dispose()


# --- Authenticated fixtures (require DESCOPE_CLIENT_ID + DESCOPE_CLIENT_SECRET) ---


@pytest.fixture(scope="session")
def access_token():
    """Get an OIDC-compatible access token via client credentials flow.
    Skips if credentials are not set.
    """
    if not os.environ.get("DESCOPE_CLIENT_ID") or not os.environ.get("DESCOPE_CLIENT_SECRET"):
        pytest.skip("DESCOPE_CLIENT_ID/DESCOPE_CLIENT_SECRET not set")
    return get_access_token_via_client_credentials()


@pytest.fixture
def auth_api_context(playwright, access_token):
    """API request context with valid auth token."""
    context = playwright.request.new_context(
        base_url=BACKEND_URL,
        extra_http_headers={
            "Authorization": f"Bearer {access_token}",
        },
    )
    yield context
    context.dispose()
