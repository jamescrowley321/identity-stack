"""E2E test configuration and fixtures.

Tests run against a live frontend (localhost:3000) + backend (localhost:8000).
Start both servers before running: `make dev-backend` and `make dev-frontend`.

Authentication uses Descope OIDC — tests that require auth use a stored
session state or skip if credentials aren't available.
"""

import os

import pytest

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
    """Create an API request context for direct API testing."""
    context = playwright.request.new_context(base_url=BACKEND_URL)
    yield context
    context.dispose()
