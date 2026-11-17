"""The Zitadel end-to-end suite: the shipped SPA and backend against the compose Zitadel.

It needs, all running and none of them optional:

- the compose Zitadel (``make zitadel-up``) with ``infra/zitadel`` applied to it,
  and the applied root's outputs (``terraform -chdir=infra/zitadel output -json``)
  in ``ZITADEL_TERRAFORM_OUTPUTS``; ``make test-e2e-zitadel`` sets it;
- the application database in ``DATABASE_URL``, migrated and seeded by
  ``make zitadel-seed`` and not yet used for a first login of the ``globex`` user;
- the backend on ``E2E_BACKEND_URL`` (default ``http://localhost:8000``) with the
  root's ``backend_environment`` and ``zitadel`` in ``CANONICAL_RBAC_PROVIDERS``
  and ``JIT_ENABLED_PROVIDERS``;
- the SPA built with the root's ``frontend_environment`` on ``E2E_FRONTEND_URL``
  (default ``http://localhost:3000``): the origin the applications redirect to.

When the outputs, the backend or the SPA are missing, every test fails; when the
database does not answer, every test that reads it fails. The suite never skips.

It lives apart from ``tests/e2e/`` because that suite's conftest fails a run
without the Descope secrets, and this one needs no secret at all.
"""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import APIRequestContext, Browser, BrowserContext, Playwright
from playwright.sync_api import Error as PlaywrightError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.e2e_zitadel.browser_login import BrowserLogin, ZitadelUser

BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:8000")
FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:3000")
OUTPUTS_VARIABLE = "ZITADEL_TERRAFORM_OUTPUTS"
_PROBE_TIMEOUT_SECONDS = 10
# The email domain of the globex test user (infra/zitadel/main.tf).
GLOBEX_DOMAIN = "globex.test"


@dataclass(frozen=True)
class Instance:
    """What the suite reads from the applied ``infra/zitadel`` root."""

    issuer: str
    spa_client_id: str
    other_client_id: str
    organization_ids: dict[str, str]
    user_ids: dict[str, str]
    users: dict[str, ZitadelUser]


def _read_outputs() -> Instance:
    raw = os.environ.get(OUTPUTS_VARIABLE, "")
    if not raw:
        pytest.fail(f"{OUTPUTS_VARIABLE} is not set: run this suite with `make test-e2e-zitadel` (docs/zitadel.md)")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError(f"expected a JSON object, got {type(parsed).__name__}")
        outputs = {name: output["value"] for name, output in parsed.items()}
        if not outputs:
            pytest.fail(f"{OUTPUTS_VARIABLE} holds no outputs: apply infra/zitadel first (docs/zitadel.md)")
        passwords = outputs["test_user_passwords"]
        return Instance(
            issuer=outputs["issuer_url"],
            spa_client_id=outputs["spa_client_id"],
            other_client_id=outputs["other_client_id"],
            organization_ids=outputs["organization_ids"],
            user_ids=outputs["test_user_ids"],
            users={
                key: ZitadelUser(login_name=login_name, password=passwords[key])
                for key, login_name in outputs["test_user_login_names"].items()
            },
        )
    except (ValueError, KeyError, TypeError) as exc:
        pytest.fail(f"{OUTPUTS_VARIABLE} is not the applied infra/zitadel root's outputs ({exc!r})")


@pytest.fixture(scope="session")
def zitadel() -> Instance:
    instance = _read_outputs()
    try:
        httpx.get(
            f"{instance.issuer}/.well-known/openid-configuration", timeout=_PROBE_TIMEOUT_SECONDS
        ).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(f"the Zitadel at {instance.issuer} does not answer ({exc!r}): run `make zitadel-up` first")
    return instance


class Database:
    """Reads and one operator write on the application database the backend uses."""

    def __init__(self, url: str) -> None:
        self.url = url

    def _run(self, statement: str, **params) -> list[tuple]:
        async def _go() -> list[tuple]:
            engine = create_async_engine(self.url)
            try:
                async with engine.begin() as connection:
                    result = await connection.execute(text(statement), params)
                    return [tuple(row) for row in result] if result.returns_rows else []
            finally:
                await engine.dispose()

        # Playwright's sync API keeps an event loop running on this thread.
        with ThreadPoolExecutor(max_workers=1) as worker:
            return worker.submit(asyncio.run, _go()).result()

    def user_ids_with_email(self, email: str) -> list[str]:
        return [str(user_id) for (user_id,) in self._run("SELECT id FROM users WHERE email = :email", email=email)]

    def tenant_ids(self) -> dict[str, str]:
        return {name: str(tenant_id) for tenant_id, name in self._run("SELECT id, name FROM tenants")}

    def set_tenant_domains(self, organization_id: str, domains: list[str]) -> int:
        rows = self._run(
            "UPDATE tenants SET domains = CAST(:domains AS json) WHERE external_org_id = :org"
            " AND external_org_provider_id = (SELECT id FROM providers WHERE name = 'zitadel') RETURNING id",
            domains=json.dumps(domains),
            org=organization_id,
        )
        return len(rows)


@pytest.fixture(scope="session")
def database(zitadel: Instance) -> Database:
    """The application database, with the globex tenant admitting its own email domain.

    ``make zitadel-seed`` gives the tenants no email domains, and a first login is
    admitted only for an email in its tenant's domains. No route sets them yet
    (#621), so the suite writes them as an operator would (docs/zitadel.md).
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        pytest.fail("DATABASE_URL is not set: the suite reads the application database the backend uses")
    database = Database(url)
    try:
        updated = database.set_tenant_domains(zitadel.organization_ids["globex"], [GLOBEX_DOMAIN])
    except (SQLAlchemyError, OSError) as exc:
        pytest.fail(f"the application database at DATABASE_URL does not answer ({exc!r})")
    if updated != 1:
        pytest.fail("no tenant is linked to the globex organization: run `make zitadel-seed` first")
    return database


@pytest.fixture(scope="session", autouse=True)
def _stack_answers(zitadel, browser: Browser):
    """Fail the whole run, never skip it, when the backend or the SPA is not up.

    The SPA is asked through Chromium and must show its own Sign In button, so
    another server answering on its port does not pass for it.
    """
    try:
        httpx.get(f"{BACKEND_URL}/api/health", timeout=_PROBE_TIMEOUT_SECONDS).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(f"the backend is not answering at {BACKEND_URL} ({exc}); see tests/e2e_zitadel/conftest.py")
    context = browser.new_context()
    try:
        page = context.new_page()
        try:
            page.goto(f"{FRONTEND_URL}/login", timeout=_PROBE_TIMEOUT_SECONDS * 1000)
            page.get_by_role("button", name="Sign In").wait_for(timeout=_PROBE_TIMEOUT_SECONDS * 1000)
        except PlaywrightError as exc:
            pytest.fail(f"the SPA is not answering at {FRONTEND_URL} ({exc}); see tests/e2e_zitadel/conftest.py")
    finally:
        context.close()


@pytest.fixture(scope="session")
def frontend_url() -> str:
    return FRONTEND_URL


@pytest.fixture(scope="session")
def browser_login(zitadel: Instance) -> BrowserLogin:
    return BrowserLogin(zitadel.issuer, FRONTEND_URL)


@pytest.fixture
def new_context(browser: Browser):
    """Fresh browser contexts, each its own Zitadel session; closed after the test."""
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
    """API request contexts on the backend, one per bearer token (or none) and headers."""
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


# --- Measured, not asserted -------------------------------------------------------

_MEASURED = pytest.StashKey[list[tuple[str, str]]]()


@pytest.fixture
def measured(request, record_property):
    """Record a result the suite reports without asserting; printed at the end of the run."""
    results = request.config.stash.setdefault(_MEASURED, [])

    def _record(name: str, value: str) -> None:
        results.append((name, value))
        record_property(name, value)

    return _record


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    results = config.stash.get(_MEASURED, [])
    if results:
        terminalreporter.section("Zitadel: measured, not asserted")
        for name, value in results:
            terminalreporter.write_line(f"{name}: {value}")
