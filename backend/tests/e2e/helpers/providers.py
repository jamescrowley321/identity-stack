"""The identity provider the shared end-to-end suite runs against, behind one interface.

``conftest.py`` asks the provider ``E2E_PROVIDER`` names (``not_applicable.py``)
for everything that differs between providers: whether the run can start, the
principals' API tokens and headers, a browser signed in to the SPA as either
of them, and the tenant and user the Descope-era tests name. Tests themselves
never call a provider's API.

The principals:

- ``OPERATOR``: changes global definitions (Descope reuses its admin);
- ``MEMBER``: signed in, with no admin role in the tenant it acts in;
- ``ADMIN``: owner or admin of the tenant it acts in.

Descope keeps exactly what the suite did before (``helpers/auth.py``): a
client-credentials token for the member, an exchanged tenant access key for the
admin, tokens injected into the SPA's session storage, the same local skips and
the same CI credential check. Keycloak and Zitadel need no credential beyond what
their applied Terraform root outputs: both principals are the fixture's
two-tenant user, signed in through the SPA's client and the provider's own
forms, naming the tenant it acts in by ``X-Tenant-ID``. They fail the run, never
skip it, when anything they need is missing.
"""

import base64
import json
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from functools import cached_property
from typing import TypeVar

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Request, expect
from playwright.sync_api import Error as PlaywrightError

from tests.e2e.helpers.api import unique_name
from tests.e2e.not_applicable import (
    DESCOPE,
    INTERNAL_ENDPOINT_SECRETS,
    KEYCLOAK,
    ZITADEL,
    missing_internal_endpoint_secrets,
)

MEMBER = "member"
ADMIN = "admin"
OPERATOR = "operator"
PRINCIPALS = (MEMBER, ADMIN)

_PROBE_TIMEOUT_SECONDS = 10
_IDENTITY = "/api/identity"

_Signed = TypeVar("_Signed")


class EarlierSignInFailed(RuntimeError):
    """A principal's sign-in failed earlier in this run, so the run does not try it again."""


class UnreadableAccessToken(ValueError):
    """The provider issued an access token the suite cannot read an ``exp`` claim from."""


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

    def attempt(self, principal: str, sign_in: Callable[[], _Signed]) -> _Signed:
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
    while ``exp`` is more than ``MARGIN_SECONDS`` away. A sign-in that fails, or
    issues a token with no readable ``exp``, is not tried again (``FailedSignIns``).
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
        token, expiry = self._failed.attempt(principal, lambda: _with_expiry(sign_in()))
        self._tokens[principal] = (token, expiry)
        return token


def _with_expiry(token: str) -> tuple[str, float]:
    return token, _expiry(token)


def _expiry(token: str) -> float:
    """The ``exp`` claim of a JWT the provider just issued, read without verifying it."""
    segments = token.split(".")
    if len(segments) != 3:
        raise UnreadableAccessToken("the provider issued an access token that is not a JWT")
    payload = segments[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError as exc:
        raise UnreadableAccessToken("the provider issued an access token that is not a JWT") from exc
    if not isinstance(claims, dict):
        raise UnreadableAccessToken("the provider issued an access token that is not a JWT")
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        raise UnreadableAccessToken("the provider issued an access token with no exp claim")
    return float(exp)


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

    def share_target(self, request: pytest.FixtureRequest) -> str:
        """A user of ``ADMIN``'s tenant other than ``ADMIN``, as a share takes them: ``MEMBER`` by default."""
        return self.user_id(request)

    def relation_target(self, request: pytest.FixtureRequest) -> str:
        """A subject the FGA admin routes write relations for.

        Descope's store keys on whatever it is given, so a fresh name each time.
        """
        return unique_name("user")

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
        if principal == OPERATOR:
            principal = ADMIN
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


# --- Keycloak and Zitadel ------------------------------------------------------------


class TwoTenantUserProvider(E2EProvider):
    """A provider whose principals are the fixture's two-tenant user, each acting in one tenant.

    The user is linked by the provider's seed with its roles: ``ADMIN`` acts in
    the tenant where it is admin, ``MEMBER`` in the one where it is viewer, each
    naming its tenant by ``X-Tenant-ID`` as the SPA does. Authorization comes
    from the canonical model, so the token is the same for both.
    """

    #: The step that links the user, named when it is not in both tenants.
    seed_step: str

    def __init__(self, backend_url: str, frontend_url: str) -> None:
        super().__init__(backend_url, frontend_url)
        self._tokens = SignedInTokens()
        self._browser: Browser | None = None
        self._memberships_missing: str | None = None

    @cached_property
    def _linked(self):
        from tests.helpers.identity_fixture import IDENTITIES

        return IDENTITIES.cases.two_tenants

    @property
    @abstractmethod
    def _scope(self) -> str:
        """The scope the user signs in with."""

    @abstractmethod
    def _sign_in(self) -> str:
        """A new access token for the linked user, through the SPA's client."""

    @abstractmethod
    def _sign_in_page(self, page: Page) -> None:
        """After the SPA's Sign In, answer the provider's forms until the SPA's root shows."""

    def _tenant_key(self, principal: str) -> str:
        role = ADMIN if principal == ADMIN else "viewer"
        (tenant,) = [tenant for tenant, held in self._linked.roles.items() if held == role]
        return tenant

    def _tenant_name(self, key: str) -> str:
        """The canonical tenant's name for fixture tenant ``key``: the seed names it after the key."""
        return key

    @cached_property
    def _tenant_ids(self) -> dict[str, str]:
        """Canonical tenant id by fixture tenant key, from the linked user's memberships.

        Both fixture tenants must be there: without the seed the linked user is
        in neither, and the run fails here naming the user, its scope, the step
        and what ``GET /api/identity`` answered. The lookup runs once: every
        later test fails with the same message.
        """
        if self._memberships_missing is not None:
            pytest.fail(f"{self._memberships_missing} (found earlier in this run)")
        identity = self._identity(self._signed_in_token())
        by_name = {membership["tenant_name"]: membership["tenant_id"] for membership in identity["tenant_memberships"]}
        keys = {self._tenant_key(ADMIN), self._tenant_key(MEMBER)}
        missing = sorted(key for key in keys if self._tenant_name(key) not in by_name)
        if missing:
            self._memberships_missing = (
                f"the linked user {self._linked.key}, both principals (signed in with scope {self._scope!r}), is not "
                f"a member of {', '.join(missing)}: run `{self.seed_step}` first. GET {_IDENTITY} answered {identity}"
            )
            pytest.fail(self._memberships_missing)
        return {key: by_name[self._tenant_name(key)] for key in keys}

    def _signed_in_token(self) -> str:
        """The linked user's token, signed in through the SPA's client (again when it is about to expire)."""
        return self._tokens.get(self._linked.key, self._sign_in)

    def access_token(self, principal: str, request: pytest.FixtureRequest) -> str:
        return self._signed_in_token()

    def headers(self, principal: str) -> dict[str, str]:
        return {"X-Tenant-ID": self._tenant_ids[self._tenant_key(principal)]}

    def signed_in_context(self, browser: Browser, principal: str, request: pytest.FixtureRequest) -> BrowserContext:
        return browser.new_context(viewport={"width": 1280, "height": 720})

    def open_app(self, page: Page, principal: str) -> None:
        """Sign In through the provider's forms, then pick the principal's tenant in the SPA's switcher."""
        page.goto(f"{self.frontend_url}/login")
        page.get_by_role("button", name="Sign In").click()
        self._sign_in_page(page)

        key = self._tenant_key(principal)
        tenant_id = self._tenant_ids[key]
        name = self._tenant_name(key)
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

    def share_target(self, request: pytest.FixtureRequest) -> str:
        """The fixture's second member of ``ADMIN``'s tenant, by email: ``ADMIN`` and ``MEMBER`` are one user.

        The provider's seed links them (``make keycloak-seed``, ``make zitadel-seed``).
        """
        from tests.helpers.identity_fixture import IDENTITIES

        user = IDENTITIES.cases.write_only
        assert user.roles.get(self._tenant_key(ADMIN)), f"{user.key} is not a member of the admin's tenant"
        return user.email

    def relation_target(self, request: pytest.FixtureRequest) -> str:
        """The share target: OpenFGA's subjects are users of the caller's tenant only."""
        return self.share_target(request)


class KeycloakProvider(TwoTenantUserProvider):
    """``keycloak-test`` with ``infra/keycloak`` applied and ``make keycloak-seed`` run.

    ``make test-e2e-keycloak`` points the run at the realm. The backend syncs to
    it (``SYNC_PROVIDER=keycloak``), so before the first test a reconciliation
    pass takes the realm's permissions, roles and the linked users' role
    assignments into the canonical model. The user signs in through the SPA's
    client with the SPA's scope, picking the organization Keycloak asks for.
    """

    name = KEYCLOAK
    seed_step = "make keycloak-seed"

    def _tenant_name(self, key: str) -> str:
        """Reconciliation gives a linked tenant its organization's name, the fixture tenant's name."""
        from tests.helpers.identity_fixture import IDENTITIES

        return IDENTITIES.tenants[key].name

    @cached_property
    def _keycloak(self):
        # Imported here: the module reads the KEYCLOAK_TEST_* target at import,
        # which only a Keycloak run sets.
        from tests.helpers import keycloak

        return keycloak

    @cached_property
    def _model(self):
        from tests.helpers import keycloak_model

        return keycloak_model

    @property
    def _scope(self) -> str:
        return self._model.SPA_SCOPE

    def check_ready(self, request: pytest.FixtureRequest) -> None:
        self._check_internal_endpoint_secrets()
        source = self._keycloak.KeycloakTokenSource()
        try:
            source.check_reachable()
        except self._keycloak.KeycloakTokenSourceUnavailable as exc:
            pytest.fail(str(exc))
        browser = request.getfixturevalue("browser")
        self._check_stack_answers(browser)
        self._browser = browser
        from tests.e2e_keycloak.realm import take_in_the_realm

        take_in_the_realm(self.backend_url)

    def _sign_in(self) -> str:
        from tests.e2e_keycloak.browser_login import BrowserLogin

        if self._browser is None:
            pytest.fail("the Keycloak provider signs in only after check_ready")
        context = self._browser.new_context()
        try:
            return (
                BrowserLogin(self._keycloak.KeycloakTokenSource())
                .authorize(
                    context,
                    self._keycloak.TARGET.users[self._linked.key],
                    client_id=self._model.SPA_CLIENT_ID,
                    scope=self._scope,
                    organization=self._tenant_key(ADMIN),
                )
                .access_token
            )
        finally:
            context.close()

    def access_token(self, principal: str, request: pytest.FixtureRequest) -> str:
        if principal != OPERATOR:
            return super().access_token(principal, request)
        from tests.helpers.keycloak_fixture import OPERATOR as operator

        return self._tokens.get(operator.key, self._sign_in_operator)

    def headers(self, principal: str) -> dict[str, str]:
        return {} if principal == OPERATOR else super().headers(principal)

    def _sign_in_operator(self) -> str:
        from tests.e2e_keycloak.browser_login import BrowserLogin
        from tests.helpers.keycloak_fixture import OPERATOR as operator

        if self._browser is None:
            pytest.fail("the Keycloak provider signs in only after check_ready")
        context = self._browser.new_context()
        try:
            return (
                BrowserLogin(self._keycloak.KeycloakTokenSource())
                .authorize(
                    context,
                    self._keycloak.TARGET.users[operator.key],
                    client_id=self._model.SPA_CLIENT_ID,
                    scope=self._scope,
                    organization=operator.home,
                )
                .access_token
            )
        finally:
            context.close()

    def _sign_in_page(self, page: Page) -> None:
        from tests.e2e_keycloak.browser_login import sign_in

        sign_in(
            page,
            self._keycloak.TARGET.users[self._linked.key],
            self.frontend_url,
            organization=self._tenant_key(ADMIN),
        )


class ZitadelProvider(TwoTenantUserProvider):
    """The compose Zitadel with ``infra/zitadel`` applied and ``make zitadel-seed`` run.

    The fixture's first-login user is left alone: ``tests/e2e_zitadel`` needs
    its first login fresh.
    """

    name = ZITADEL
    seed_step = "make zitadel-seed"

    def access_token(self, principal: str, request: pytest.FixtureRequest) -> str:
        if principal == OPERATOR:
            pytest.fail("Zitadel has no seeded operator principal yet (#710, #709)")
        return super().access_token(principal, request)

    @cached_property
    def _instance(self):
        from tests.e2e_zitadel.instance import read_outputs

        return read_outputs()

    @property
    def _scope(self) -> str:
        return self._instance.spa_scope

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
                    scope=self._scope,
                )
                .access_token
            )
        finally:
            context.close()

    def _sign_in_page(self, page: Page) -> None:
        from tests.e2e_zitadel.browser_login import sign_in

        sign_in(page, self._instance.users[self._linked.key], self.frontend_url)


PROVIDER_CLASSES: dict[str, type[E2EProvider]] = {
    DESCOPE: DescopeProvider,
    KEYCLOAK: KeycloakProvider,
    ZITADEL: ZitadelProvider,
}
