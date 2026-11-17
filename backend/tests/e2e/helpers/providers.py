"""The identity provider the shared end-to-end suite runs against, behind one interface.

``conftest.py`` asks the provider ``E2E_PROVIDER`` names (``not_applicable.py``)
for everything that differs between providers: whether the run can start, the
two principals' API tokens and headers, a browser signed in to the SPA as either
of them, and the tenant and user the Descope-era tests name. Tests themselves
never call a provider's API.

The two principals:

- ``MEMBER``: signed in, with no admin role in the tenant it acts in;
- ``ADMIN``: owner or admin of the tenant it acts in.

Descope keeps exactly what the suite did before (``helpers/auth.py``): a
client-credentials token for the member, an exchanged tenant access key for the
admin, tokens injected into the SPA's session storage, the same local skips and
the same CI credential check. Keycloak and Zitadel need no credential beyond what
their applied Terraform root outputs, sign in through the SPA and the provider's
own forms, and fail the run, never skip it, when anything they need is missing.
"""

import base64
import json
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from functools import cached_property

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Request, expect
from playwright.sync_api import Error as PlaywrightError

from tests.e2e.not_applicable import (
    DESCOPE,
    INTERNAL_ENDPOINT_SECRETS,
    KEYCLOAK,
    KEYCLOAK_SPA_ADMIN,
    ZITADEL,
    missing_internal_endpoint_secrets,
)

MEMBER = "member"
ADMIN = "admin"
PRINCIPALS = (MEMBER, ADMIN)

_PROBE_TIMEOUT_SECONDS = 10
_IDENTITY = "/api/identity"


class EarlierSignInFailed(RuntimeError):
    """A principal's sign-in failed earlier in this run, so the run does not try it again."""


class FailedSignIns:
    """Each principal signs in until its first failure; later attempts fail at once.

    The token fixtures are per test, so without this a failing sign-in would be
    retried by every test: for Descope's admin that is a new owner+admin access
    key on the shared project per test, each able to wait out a timeout. A pytest
    skip or fail raised by ``sign_in`` is an outcome, not a failure, and passes
    through unrecorded.
    """

    def __init__(self) -> None:
        self._failures: dict[str, Exception] = {}

    def attempt(self, principal: str, sign_in: Callable[[], str]) -> str:
        failure = self._failures.get(principal)
        if failure is not None:
            raise EarlierSignInFailed(
                f"signing in as the {principal} principal failed earlier in this run: {failure!r}"
            ) from failure
        try:
            return sign_in()
        except Exception as exc:
            self._failures[principal] = exc
            raise


class SignedInTokens:
    """Each principal's access token, signed in again once it is about to expire.

    Keycloak (300 s) and Zitadel (15 min) tokens expire within a slow run, so the
    suite asks for a principal's token per test and gets the one it already has
    while ``exp`` is more than ``MARGIN_SECONDS`` away. A sign-in that fails is
    not tried again (``FailedSignIns``).
    """

    MARGIN_SECONDS = 60

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._tokens: dict[str, tuple[str, float]] = {}
        self._failed = FailedSignIns()

    def get(self, principal: str, sign_in: Callable[[], str]) -> str:
        held = self._tokens.get(principal)
        if held is not None and held[1] - self._clock() > self.MARGIN_SECONDS:
            return held[0]
        token = self._failed.attempt(principal, sign_in)
        self._tokens[principal] = (token, _expiry(token))
        return token


def _expiry(token: str) -> float:
    """The ``exp`` claim of a JWT the provider just issued, read without verifying it."""
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if not isinstance(claims.get("exp"), int | float):
        pytest.fail("the provider issued an access token with no exp claim")
    return float(claims["exp"])


class E2EProvider(ABC):
    """What the suite needs from the provider it runs against."""

    name: str

    def __init__(self, backend_url: str, frontend_url: str) -> None:
        self.backend_url = backend_url
        self.frontend_url = frontend_url

    @abstractmethod
    def check_ready(self, request: pytest.FixtureRequest) -> None:
        """Fail the run when what the provider's tests need is missing."""

    def sweep(self) -> None:
        """Remove what an earlier run of the suite left on the provider."""

    @abstractmethod
    def access_token(self, principal: str, request: pytest.FixtureRequest) -> str:
        """A bearer token the backend accepts for ``principal``."""

    def headers(self, principal: str) -> dict[str, str]:
        """Headers ``principal`` sends beside its token on every API request."""
        return {}

    @abstractmethod
    def signed_in_context(self, browser: Browser, principal: str, request: pytest.FixtureRequest) -> BrowserContext:
        """A browser context in which ``open_app`` shows the SPA signed in as ``principal``."""

    @abstractmethod
    def open_app(self, page: Page, principal: str) -> None:
        """Bring the SPA up on its root page, signed in as ``principal``."""

    @abstractmethod
    def tenant_id(self, request: pytest.FixtureRequest) -> str:
        """The tenant ``ADMIN`` administers."""

    @abstractmethod
    def user_id(self, request: pytest.FixtureRequest) -> str:
        """The id the backend's routes take for ``MEMBER``."""

    # --- shared by the providers that sign in through the SPA ---------------------

    def _check_internal_endpoint_secrets(self) -> None:
        """Fail when the internal-endpoint tests would skip: they read these secrets from the environment."""
        missing = missing_internal_endpoint_secrets(self.name)
        if missing:
            pytest.fail(
                f"the {self.name} run needs {', '.join(INTERNAL_ENDPOINT_SECRETS)}, the same values the "
                f"backend has; unset: {', '.join(missing)}"
            )

    def _check_stack_answers(self, browser: Browser) -> None:
        """Fail when the backend or the SPA does not answer; the SPA must show its own Sign In."""
        try:
            httpx.get(f"{self.backend_url}/api/health", timeout=_PROBE_TIMEOUT_SECONDS).raise_for_status()
        except httpx.HTTPError as exc:
            pytest.fail(f"the backend is not answering at {self.backend_url} ({exc}); see docs/local-development.md")
        context = browser.new_context()
        try:
            page = context.new_page()
            try:
                page.goto(f"{self.frontend_url}/login", timeout=_PROBE_TIMEOUT_SECONDS * 1000)
                page.get_by_role("button", name="Sign In").wait_for(timeout=_PROBE_TIMEOUT_SECONDS * 1000)
            except PlaywrightError as exc:
                pytest.fail(f"the SPA is not answering at {self.frontend_url} ({exc}); see docs/local-development.md")
        finally:
            context.close()

    def _identity(self, token: str, headers: dict[str, str] | None = None) -> dict:
        response = httpx.get(
            f"{self.backend_url}{_IDENTITY}",
            headers={"Authorization": f"Bearer {token}", **(headers or {})},
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        if response.status_code != httpx.codes.OK:
            pytest.fail(
                f"GET {_IDENTITY} for the {self.name} principal answered {response.status_code}: {response.text}"
            )
        return response.json()


# --- Descope -------------------------------------------------------------------------

#: Every credential the CI E2E job passes in. Locally these may be absent and the
#: authenticated fixtures skip; in CI their absence is the failure this suite spent
#: months hiding — 164 of 220 tests skipped on empty credentials while the job
#: reported green. The workflow gates the step on DESCOPE_PROJECT_ID alone, so any
#: OTHER variable going missing (renamed secret, expired client secret removed from
#: the repo) silently re-hollows the suite.
#
# E2E_ORY_ISSUER_URL and E2E_DATABASE_URL are listed for the same reason, one step
# further on: test_ory_sso_e2e.py skips its whole module without the first and its
# tier-3 authorization test without the second, so either going missing would
# silently retire the Ory end-to-end coverage (FR-21) while the job stayed green.
DESCOPE_CI_REQUIRED_ENV = (
    "DESCOPE_PROJECT_ID",
    "DESCOPE_MANAGEMENT_KEY",
    "DESCOPE_CLIENT_ID",
    "DESCOPE_CLIENT_SECRET",
    "E2E_TEST_EMAIL",
    "E2E_TEST_TENANT_ID",
    "E2E_ORY_ISSUER_URL",
    "E2E_DATABASE_URL",
)


class DescopeProvider(E2EProvider):
    """The suite as it ran before it had a provider setting."""

    name = DESCOPE

    def __init__(self, backend_url: str, frontend_url: str) -> None:
        super().__init__(backend_url, frontend_url)
        self._tokens: dict[str, str] = {}
        self._failed = FailedSignIns()

    @property
    def _has_mgmt_key(self) -> bool:
        return bool(os.environ.get("DESCOPE_MANAGEMENT_KEY"))

    @property
    def _has_client_creds(self) -> bool:
        return bool(os.environ.get("DESCOPE_CLIENT_ID") and os.environ.get("DESCOPE_CLIENT_SECRET"))

    def check_ready(self, request: pytest.FixtureRequest) -> None:
        """In CI, a missing credential fails the run instead of skipping the suite."""
        if not os.environ.get("CI"):
            return
        missing = [name for name in DESCOPE_CI_REQUIRED_ENV if not os.environ.get(name)]
        if missing:
            pytest.fail(
                "E2E credentials missing in CI: "
                + ", ".join(missing)
                + ". The authenticated tests would skip and the job would report green; "
                "fix the secret rather than letting the gate go hollow."
            )

    def sweep(self) -> None:
        """Remove users, the per-session admin access key and the RBAC objects runs strand.

        Each sweep is independent: one failing must not stop the others, since
        they leak for different reasons and at different rates.
        """
        if not self._has_mgmt_key:
            return
        from tests.e2e.helpers.auth import sweep_leaked_e2e_access_keys, sweep_leaked_e2e_rbac, sweep_leaked_e2e_users

        for sweep in (sweep_leaked_e2e_users, sweep_leaked_e2e_access_keys, sweep_leaked_e2e_rbac):
            try:
                sweep()
            # A sweep must never fail the run.
            except Exception as exc:
                print(f"[E2E] {sweep.__name__} failed: {exc!r}")

    def ensure_test_user(self) -> dict:
        if not self._has_mgmt_key:
            pytest.skip("DESCOPE_MANAGEMENT_KEY not set")
        from tests.e2e.helpers.auth import ensure_test_user

        return ensure_test_user()

    def access_token(self, principal: str, request: pytest.FixtureRequest) -> str:
        """One token per principal for the whole run, as the suite always had.

        A failed sign-in is tried once per run too, as when the token fixtures
        were session-scoped; a skip (credential not set) still skips each test.
        """
        if principal not in self._tokens:
            self._tokens[principal] = self._failed.attempt(principal, lambda: self._new_token(principal))
        return self._tokens[principal]

    def _new_token(self, principal: str) -> str:
        from tests.e2e.helpers.auth import get_admin_session_token, get_oidc_access_token

        if principal == ADMIN:
            # A tenant-scoped access key with owner+admin, exchanged for a session
            # token: it passes OIDC validation AND carries the dct/tenants claims
            # require_role() reads.
            if not self._has_mgmt_key:
                pytest.skip("DESCOPE_MANAGEMENT_KEY not set")
            return get_admin_session_token()
        # Client credentials: an OIDC-compatible token with no tenant claims.
        if not self._has_client_creds:
            pytest.skip("DESCOPE_CLIENT_ID/DESCOPE_CLIENT_SECRET not set")
        return get_oidc_access_token()

    def signed_in_context(self, browser: Browser, principal: str, request: pytest.FixtureRequest) -> BrowserContext:
        """The principal's token injected into sessionStorage before first paint."""
        from tests.e2e.helpers.auth import create_authenticated_context

        request.getfixturevalue("_ensure_test_user")
        token = request.getfixturevalue("admin_access_token" if principal == ADMIN else "auth_access_token")
        return create_authenticated_context(browser, self.frontend_url, token)

    def open_app(self, page: Page, principal: str) -> None:
        page.goto(self.frontend_url + "/")

    def tenant_id(self, request: pytest.FixtureRequest) -> str:
        tid = os.environ.get("E2E_TEST_TENANT_ID", "")
        if not tid:
            pytest.skip("E2E_TEST_TENANT_ID not set")
        return tid

    def user_id(self, request: pytest.FixtureRequest) -> str:
        """The Descope login ID (email) of the E2E test user.

        Uses loginIds[0] (the user's email) because the Descope Management API's
        /v1/mgmt/user/update/role/add expects loginId, not the internal userId.
        """
        user = request.getfixturevalue("_ensure_test_user")
        login_ids = user.get("loginIds", [])
        if login_ids:
            return login_ids[0]
        user_id = user.get("userId", "")
        if not user_id:
            pytest.fail("Descope returned a test user with neither loginIds nor userId")
        return user_id


# --- Keycloak ------------------------------------------------------------------------


class KeycloakProvider(E2EProvider):
    """``keycloak-test`` with ``infra/keycloak`` applied; ``make test-e2e-keycloak`` points the run at it.

    ``MEMBER`` is the fixture's member case on the SPA's app client
    (``member-app``), ``ADMIN`` its tenant admin on ``tenant-admin-app``; both
    sign in to the acme organization, whose canonical tenant is the fixture key.
    """

    name = KEYCLOAK

    def __init__(self, backend_url: str, frontend_url: str) -> None:
        super().__init__(backend_url, frontend_url)
        self._tokens = SignedInTokens()

    @cached_property
    def _keycloak(self):
        # Imported here: the module reads the KEYCLOAK_TEST_* target at import,
        # which only a Keycloak run sets.
        from tests.helpers import keycloak

        return keycloak

    def _user(self, principal: str):
        return self._keycloak.DAVE if principal == ADMIN else self._keycloak.ALICE

    def _client(self, principal: str) -> str:
        return self._keycloak.TENANT_ADMIN_APP_CLIENT if principal == ADMIN else self._keycloak.MEMBER_APP_CLIENT

    def check_ready(self, request: pytest.FixtureRequest) -> None:
        self._check_internal_endpoint_secrets()
        source = self._keycloak.KeycloakTokenSource()
        try:
            source.check_reachable()
        except self._keycloak.KeycloakTokenSourceUnavailable as exc:
            pytest.fail(str(exc))
        self._check_stack_answers(request.getfixturevalue("browser"))

    def access_token(self, principal: str, request: pytest.FixtureRequest) -> str:
        def sign_in() -> str:
            return (
                self._keycloak.KeycloakTokenSource()
                .login(self._user(principal), client_id=self._client(principal), scope=self._keycloak.ACME_SCOPE)
                .access_token
            )

        return self._tokens.get(principal, sign_in)

    @staticmethod
    def _member_app_only(principal: str) -> None:
        """The SPA signs in through member-app: a page as the tenant admin would be a member's session."""
        if principal == ADMIN:
            pytest.fail(f"no Keycloak admin page: {KEYCLOAK_SPA_ADMIN}; list the test in tests/e2e/not_applicable.py")

    def signed_in_context(self, browser: Browser, principal: str, request: pytest.FixtureRequest) -> BrowserContext:
        self._member_app_only(principal)
        return browser.new_context(viewport={"width": 1280, "height": 720})

    def open_app(self, page: Page, principal: str) -> None:
        """Sign In on the SPA's login page, then Keycloak's identity-first form."""
        self._member_app_only(principal)
        user = self._user(principal)
        page.goto(f"{self.frontend_url}/login")
        page.get_by_role("button", name="Sign In").click()
        page.locator('input[name="username"]').fill(user.username)
        page.locator('input[name="username"]').press("Enter")
        page.locator('input[name="password"]').fill(user.password)
        page.locator('input[name="password"]').press("Enter")
        page.wait_for_url(f"{self.frontend_url}/")

    def tenant_id(self, request: pytest.FixtureRequest) -> str:
        identity = self._identity(self.access_token(ADMIN, request))
        return identity["token_tenant"]["tenant_id"]

    def user_id(self, request: pytest.FixtureRequest) -> str:
        return self._identity(self.access_token(MEMBER, request))["user"]["id"]


# --- Zitadel -------------------------------------------------------------------------


class ZitadelProvider(E2EProvider):
    """The compose Zitadel with ``infra/zitadel`` applied and ``make zitadel-seed`` run.

    Both principals are the fixture's two-tenant user, linked by the seed with
    its roles: ``ADMIN`` acts in the tenant where it is admin, ``MEMBER`` in the
    one where it is viewer, each naming its tenant by ``X-Tenant-ID`` as the SPA
    does. The fixture's first-login user is left alone: ``tests/e2e_zitadel``
    needs its first login fresh.
    """

    name = ZITADEL

    def __init__(self, backend_url: str, frontend_url: str) -> None:
        super().__init__(backend_url, frontend_url)
        self._tokens = SignedInTokens()
        self._browser: Browser | None = None

    @cached_property
    def _instance(self):
        from tests.e2e_zitadel.instance import read_outputs

        return read_outputs()

    @cached_property
    def _linked(self):
        from tests.helpers.identity_fixture import IDENTITIES

        return IDENTITIES.cases.two_tenants

    def _tenant_key(self, principal: str) -> str:
        role = ADMIN if principal == ADMIN else "viewer"
        (tenant,) = [tenant for tenant, held in self._linked.roles.items() if held == role]
        return tenant

    @cached_property
    def _tenant_ids(self) -> dict[str, str]:
        """Canonical tenant id by fixture tenant key, from the linked user's memberships."""
        identity = self._identity(self._signed_in_token())
        return {membership["tenant_name"]: membership["tenant_id"] for membership in identity["tenant_memberships"]}

    def check_ready(self, request: pytest.FixtureRequest) -> None:
        self._check_internal_endpoint_secrets()
        instance = self._instance
        try:
            httpx.get(
                f"{instance.issuer}/.well-known/openid-configuration", timeout=_PROBE_TIMEOUT_SECONDS
            ).raise_for_status()
        except httpx.HTTPError as exc:
            pytest.fail(f"the Zitadel at {instance.issuer} does not answer ({exc!r}): run `make zitadel-up` first")
        browser = request.getfixturevalue("browser")
        self._check_stack_answers(browser)
        self._browser = browser

    def _signed_in_token(self) -> str:
        """The linked user's token, signed in through the SPA's application (again when it is about to expire)."""
        return self._tokens.get(self._linked.key, self._sign_in)

    def _sign_in(self) -> str:
        from tests.e2e_zitadel.browser_login import BrowserLogin

        if self._browser is None:
            pytest.fail("the Zitadel provider signs in only after check_ready")
        context = self._browser.new_context()
        try:
            return (
                BrowserLogin(self._instance.issuer, self.frontend_url)
                .authorize(
                    context,
                    self._instance.users[self._linked.key],
                    client_id=self._instance.spa_client_id,
                    scope=self._instance.spa_scope,
                )
                .access_token
            )
        finally:
            context.close()

    def access_token(self, principal: str, request: pytest.FixtureRequest) -> str:
        return self._signed_in_token()

    def headers(self, principal: str) -> dict[str, str]:
        return {"X-Tenant-ID": self._tenant_ids[self._tenant_key(principal)]}

    def signed_in_context(self, browser: Browser, principal: str, request: pytest.FixtureRequest) -> BrowserContext:
        return browser.new_context(viewport={"width": 1280, "height": 720})

    def open_app(self, page: Page, principal: str) -> None:
        """Sign In through Login V2, then pick the principal's tenant in the SPA's switcher."""
        from tests.e2e_zitadel.browser_login import sign_in

        page.goto(f"{self.frontend_url}/login")
        page.get_by_role("button", name="Sign In").click()
        sign_in(page, self._instance.users[self._linked.key], self.frontend_url)

        name = self._tenant_key(principal)
        tenant_id = self._tenant_ids[name]
        switcher = page.locator("header").get_by_role("combobox")
        expect(switcher).to_be_visible()
        if switcher.inner_text() != name:
            switcher.click()
            page.get_by_role("option", name=name).click()
        expect(switcher).to_have_text(name)

        def _names_the_tenant(request: Request) -> bool:
            return "/api/" in request.url and request.headers.get("x-tenant-id") == tenant_id

        with page.expect_request(_names_the_tenant):
            page.goto(f"{self.frontend_url}/")

    def tenant_id(self, request: pytest.FixtureRequest) -> str:
        return self._tenant_ids[self._tenant_key(ADMIN)]

    def user_id(self, request: pytest.FixtureRequest) -> str:
        return self._identity(self._signed_in_token())["user"]["id"]


PROVIDER_CLASSES: dict[str, type[E2EProvider]] = {
    DESCOPE: DescopeProvider,
    KEYCLOAK: KeycloakProvider,
    ZITADEL: ZitadelProvider,
}
