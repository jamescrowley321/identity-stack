"""E2E test configuration and fixtures.

The suite runs against the identity provider ``E2E_PROVIDER`` names (Descope
when unset): ``helpers/providers.py`` holds everything that differs between
providers, and ``not_applicable.py`` the one table of tests a provider's run
leaves out, with the reason. A left-out test is deselected and listed at the end
of the run; it is never skipped by environment.
"""

import os
from pathlib import Path

import pytest

from tests.e2e.helpers.pages import signed_in_page
from tests.e2e.helpers.providers import ADMIN, MEMBER, OPERATOR, PROVIDER_CLASSES, E2EProvider
from tests.e2e.not_applicable import SUITE, UnknownE2EProvider, reason_not_applicable, selected_provider

FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:3000")
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:8000")

_SUITE_DIR = Path(__file__).parent
_NOT_APPLICABLE = pytest.StashKey[list[tuple[str, str]]]()


def _provider_name() -> str:
    try:
        return selected_provider(os.environ)
    except UnknownE2EProvider as exc:
        raise pytest.UsageError(str(exc)) from exc


def pytest_configure(config):
    """Refuse an unknown ``E2E_PROVIDER`` once, before any module is imported."""
    _provider_name()


def pytest_collection_modifyitems(config, items):
    """Deselect the tests ``NOT_APPLICABLE`` lists for this run's provider."""
    provider = _provider_name()
    kept, left_out, listed = [], [], []
    for item in items:
        try:
            module = item.path.relative_to(_SUITE_DIR).as_posix()
        except ValueError:
            kept.append(item)
            continue
        _path, _, rest = item.nodeid.partition("::")
        reason = reason_not_applicable(f"{SUITE}{module}::{rest}", provider)
        if reason is None:
            kept.append(item)
        else:
            left_out.append(item)
            listed.append((item.nodeid, reason))
    if left_out:
        config.hook.pytest_deselected(items=left_out)
        items[:] = kept
    config.stash[_NOT_APPLICABLE] = listed


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    listed = config.stash.get(_NOT_APPLICABLE, [])
    if listed:
        terminalreporter.section(f"not applicable to {_provider_name()} (tests/e2e/not_applicable.py)")
        for nodeid, reason in listed:
            terminalreporter.write_line(f"{nodeid}: {reason}")


@pytest.fixture(scope="session")
def e2e_provider() -> E2EProvider:
    return PROVIDER_CLASSES[_provider_name()](BACKEND_URL, FRONTEND_URL)


@pytest.fixture(scope="session", autouse=True)
def _provider_is_ready(e2e_provider, request):
    """Fail the run, never skip it, when what the provider's tests need is missing."""
    e2e_provider.check_ready(request)


@pytest.fixture(scope="session", autouse=True)
def _sweep_leaked_users(e2e_provider, _provider_is_ready):
    """Remove objects stranded by this or an earlier E2E run.

    Runs on the way in as well as out: a run that is cancelled or killed never
    reaches its teardown, so sweeping only at the end would let them accumulate
    until someone noticed the project was at its user limit.
    """
    e2e_provider.sweep()
    yield
    e2e_provider.sweep()


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
def _ensure_test_user(e2e_provider):
    """The Descope test user, created when missing (Descope runs only)."""
    return e2e_provider.ensure_test_user()


@pytest.fixture
def auth_access_token(e2e_provider, request):
    """A token for a signed-in principal with no admin role in its tenant (the provider keeps it fresh)."""
    return e2e_provider.access_token(MEMBER, request)


@pytest.fixture
def auth_api_context(playwright, e2e_provider, auth_access_token):
    """API request context with the member's token (and tenant header, where the provider needs one)."""
    context = playwright.request.new_context(
        base_url=BACKEND_URL,
        extra_http_headers={"Authorization": f"Bearer {auth_access_token}", **e2e_provider.headers(MEMBER)},
    )
    yield context
    context.dispose()


@pytest.fixture
def admin_access_token(e2e_provider, request):
    """A token for an owner/admin of the test tenant, carrying what require_role() reads (kept fresh)."""
    return e2e_provider.access_token(ADMIN, request)


@pytest.fixture
def admin_api_context(playwright, e2e_provider, admin_access_token):
    """API request context with the admin's token (and tenant header, where the provider needs one)."""
    context = playwright.request.new_context(
        base_url=BACKEND_URL,
        extra_http_headers={"Authorization": f"Bearer {admin_access_token}", **e2e_provider.headers(ADMIN)},
    )
    yield context
    context.dispose()


@pytest.fixture
def operator_access_token(e2e_provider, request):
    """A principal allowed to change role and permission definitions."""
    return e2e_provider.access_token(OPERATOR, request)


@pytest.fixture
def operator_api_context(playwright, e2e_provider, operator_access_token):
    context = playwright.request.new_context(
        base_url=BACKEND_URL,
        extra_http_headers={"Authorization": f"Bearer {operator_access_token}", **e2e_provider.headers(OPERATOR)},
    )
    yield context
    context.dispose()


@pytest.fixture(scope="session")
def test_user_id(e2e_provider, request) -> str:
    """The id the backend's routes take for the member principal (Descope: its login ID)."""
    return e2e_provider.user_id(request)


@pytest.fixture(scope="session")
def share_target(e2e_provider, request) -> str:
    """A user of the admin's tenant, other than the admin, that a document can be shared with."""
    return e2e_provider.share_target(request)


@pytest.fixture
def relation_target(e2e_provider, request) -> str:
    """A subject the FGA admin routes write relations for (``E2EProvider.relation_target``)."""
    return e2e_provider.relation_target(request)


@pytest.fixture(scope="session")
def test_tenant_id(e2e_provider, request) -> str:
    """The tenant the admin principal administers."""
    return e2e_provider.tenant_id(request)


# --- Failure evidence for browser tests -------------------------------------
#
# The signed-in page fixtures below capture what the page showed when a test or
# their own set-up fails (``helpers/pages.py``) into E2E_ARTIFACT_DIR.

_ARTIFACT_DIR = os.environ.get("E2E_ARTIFACT_DIR", "")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Record each phase's report on the item so fixtures can see the outcome."""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"_report_{report.when}", report)


@pytest.fixture
def admin_page(browser, e2e_provider, request):
    """Browser page signed in to the SPA as an admin/owner of the test tenant.

    ``auth_page`` is the member, for which ``useRBAC`` reports isAdmin=false and
    every admin-gated card stays unrendered. A test that creates something through
    the admin API and then looks for it in the UI needs the browser session to be
    that same admin, or it is asserting against a page the identity can never see.
    """
    yield from signed_in_page(browser, e2e_provider, ADMIN, request, "admin_page", _ARTIFACT_DIR)


@pytest.fixture
def auth_page(browser, e2e_provider, request):
    """Browser page signed in to the SPA as the member principal.

    Descope injects the client-credentials token into sessionStorage via
    add_init_script, so react-oidc-context treats the session as authenticated on
    first render; Keycloak and Zitadel sign in through their own forms.
    """
    yield from signed_in_page(browser, e2e_provider, MEMBER, request, "auth_page", _ARTIFACT_DIR)
