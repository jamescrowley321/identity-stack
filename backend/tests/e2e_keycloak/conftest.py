"""The Keycloak end-to-end suite: the shipped SPA and backend against ``keycloak-test``.

It needs, all running and none of them optional:

- ``keycloak-test`` (``make test-up-keycloak``), its issuer pinned to loopback,
  with ``infra/keycloak/`` applied to it; ``make test-e2e-keycloak`` sets the
  ``KEYCLOAK_TEST_*`` variables (``tests/helpers/keycloak_target.py``) from the
  root's outputs;
- the backend on ``E2E_BACKEND_URL`` (default ``http://localhost:8000``),
  configured from the root's ``backend_environment`` with ``keycloak`` in
  ``CANONICAL_RBAC_PROVIDERS`` and ``JIT_ENABLED_PROVIDERS`` and
  ``SYNC_PROVIDER=keycloak`` (the event pull runs), on a database ``make
  keycloak-seed`` linked to the realm;
- the SPA, built from the root's ``frontend_environment`` for
  ``identity-stack-spa``, on ``http://localhost:3000``: the only origin the
  realm's clients redirect to.

When any of them does not answer, every test fails: the suite never skips.

It lives apart from ``tests/e2e/`` because that suite's conftest fails a CI run
without the Descope secrets, and this one needs no secret at all.
"""

import os

import httpx
import pytest
from playwright.sync_api import APIRequestContext, Browser, BrowserContext, Playwright
from playwright.sync_api import Error as PlaywrightError

from tests.e2e_keycloak.browser_login import BrowserLogin
from tests.e2e_keycloak.realm import take_in_the_realm
from tests.helpers.keycloak import KEYCLOAK_REDIRECT_URI, KeycloakTokenSource, KeycloakTokenSourceUnavailable

BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:8000")
# Fixed by the realm: its clients accept no other redirect URI.
FRONTEND_URL = KEYCLOAK_REDIRECT_URI
_PROBE_TIMEOUT_SECONDS = 10


@pytest.fixture(scope="session")
def keycloak() -> KeycloakTokenSource:
    source = KeycloakTokenSource()
    try:
        source.check_reachable()
    except KeycloakTokenSourceUnavailable as exc:
        pytest.fail(str(exc))
    return source


@pytest.fixture(scope="session", autouse=True)
def _stack_answers(keycloak, browser: Browser):
    """Fail the whole run, never skip it, when the backend or the SPA is not up; then take the realm in.

    The SPA is asked through Chromium and must show its own Sign In button, so
    another server answering on its port does not pass for it.
    """
    try:
        httpx.get(f"{BACKEND_URL}/api/health", timeout=_PROBE_TIMEOUT_SECONDS).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(f"the backend is not answering at {BACKEND_URL} ({exc}); see tests/e2e_keycloak/conftest.py")
    context = browser.new_context()
    try:
        page = context.new_page()
        try:
            page.goto(f"{FRONTEND_URL}/login", timeout=_PROBE_TIMEOUT_SECONDS * 1000)
            page.get_by_role("button", name="Sign In").wait_for(timeout=_PROBE_TIMEOUT_SECONDS * 1000)
        except PlaywrightError as exc:
            pytest.fail(f"the SPA is not answering at {FRONTEND_URL} ({exc}); see tests/e2e_keycloak/conftest.py")
    finally:
        context.close()
    take_in_the_realm(BACKEND_URL)


@pytest.fixture(scope="session")
def frontend_url() -> str:
    return FRONTEND_URL


@pytest.fixture(scope="session")
def browser_login(keycloak) -> BrowserLogin:
    return BrowserLogin(keycloak)


@pytest.fixture
def new_context(browser: Browser):
    """Fresh browser contexts, each its own Keycloak session; closed after the test."""
    contexts: list[BrowserContext] = []

    def _make() -> BrowserContext:
        context = browser.new_context()
        contexts.append(context)
        return context

    yield _make
    for context in contexts:
        context.close()


@pytest.fixture
def api(playwright: Playwright):
    """API request contexts on the backend, one per bearer token (or none)."""
    contexts: list[APIRequestContext] = []

    def _make(token: str | None = None, headers: dict[str, str] | None = None) -> APIRequestContext:
        sent = dict(headers or {})
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        context = playwright.request.new_context(base_url=BACKEND_URL, extra_http_headers=sent)
        contexts.append(context)
        return context

    yield _make
    for context in contexts:
        context.dispose()
