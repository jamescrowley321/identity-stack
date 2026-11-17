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

from tests.e2e.helpers.providers import ADMIN, MEMBER, PROVIDER_CLASSES, E2EProvider
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


@pytest.fixture(scope="session")
def test_user_id(e2e_provider, request) -> str:
    """The id the backend's routes take for the member principal (Descope: its login ID)."""
    return e2e_provider.user_id(request)


@pytest.fixture(scope="session")
def test_tenant_id(e2e_provider, request) -> str:
    """The tenant the admin principal administers."""
    return e2e_provider.tenant_id(request)


# --- Failure evidence for browser tests -------------------------------------
#
# pytest-playwright's --screenshot/--tracing only cover ITS `page` fixture. The
# authenticated fixtures below build their own context (they have to: the OIDC
# session is injected via add_init_script before first paint), so those pages
# are invisible to it and a UI failure reports only which locator it waited for
# — never what the page actually showed. Capture it here instead.

_ARTIFACT_DIR = os.environ.get("E2E_ARTIFACT_DIR", "")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Record each phase's report on the item so fixtures can see the outcome."""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"_report_{report.when}", report)


def _record_console(page) -> list[str]:
    """Collect console output and uncaught errors for the life of the page.

    An SPA that throws during mount leaves `<div id="root"></div>` and nothing
    else, so the DOM says only that nothing rendered. The reason is in the
    console, which is otherwise discarded when the context closes.
    """
    messages: list[str] = []
    page.on("console", lambda m: messages.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: messages.append(f"pageerror: {e}"))
    return messages


def _capture_on_failure(page, request, label: str, console: list[str] | None = None) -> None:
    """Save a screenshot, the rendered HTML, and the console if the test failed."""
    if not _ARTIFACT_DIR:
        return
    report = getattr(request.node, "_report_call", None)
    if report is None or not report.failed:
        return
    out = Path(_ARTIFACT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{request.node.name}-{label}".replace("/", "_").replace("::", "-")
    try:
        page.screenshot(path=str(out / f"{stem}.png"), full_page=True)
        (out / f"{stem}.html").write_text(page.content(), encoding="utf-8")
        (out / f"{stem}.url.txt").write_text(page.url, encoding="utf-8")
        if console:
            (out / f"{stem}.console.txt").write_text("\n".join(console), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - diagnostics must never fail a test
        print(f"[e2e] could not capture failure evidence for {stem}: {exc}")


def _signed_in_page(browser, e2e_provider, principal, request, label):
    context = e2e_provider.signed_in_context(browser, principal, request)
    page = context.new_page()
    console = _record_console(page)
    e2e_provider.open_app(page, principal)
    page.wait_for_load_state("networkidle")
    yield page
    _capture_on_failure(page, request, label, console)
    context.close()


@pytest.fixture
def admin_page(browser, e2e_provider, request):
    """Browser page signed in to the SPA as an admin/owner of the test tenant.

    ``auth_page`` is the member, for which ``useRBAC`` reports isAdmin=false and
    every admin-gated card stays unrendered. A test that creates something through
    the admin API and then looks for it in the UI needs the browser session to be
    that same admin, or it is asserting against a page the identity can never see.
    """
    yield from _signed_in_page(browser, e2e_provider, ADMIN, request, "admin_page")


@pytest.fixture
def auth_page(browser, e2e_provider, request):
    """Browser page signed in to the SPA as the member principal.

    Descope injects the client-credentials token into sessionStorage via
    add_init_script, so react-oidc-context treats the session as authenticated on
    first render; Keycloak and Zitadel sign in through their own forms.
    """
    yield from _signed_in_page(browser, e2e_provider, MEMBER, request, "auth_page")
