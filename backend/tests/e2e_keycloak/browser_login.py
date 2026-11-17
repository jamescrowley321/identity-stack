"""Keycloak logins driven through Chromium, for the cases the SPA cannot ask for.

The SPA logs in through one app client with one scope. The suite also needs a
token from ``tenant-admin-app``, a token for an organization the SPA would not
offer, and a token for a user the backend refuses. Those logins run the realm's
authorization-code flow with PKCE S256 in a browser context, through the same
login form a person fills in, up to the redirect back to the SPA's origin. The
``code`` on that redirect is exchanged at the token endpoint, as the SPA would.
The SPA page it lands on finds no sign-in of its own for the ``state`` and
exchanges nothing.

Keycloak sets its session cookies ``Secure`` on a plain-HTTP origin. Chromium
keeps them for a ``*.localhost`` host, so a second authorize request in the same
context runs on the live session.
"""

import base64
import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import BrowserContext, Page, Request

from tests.helpers.keycloak import KEYCLOAK_REDIRECT_URI, KeycloakTokenSource, KeycloakUser

# A form step that does not leave the page within this long has failed.
_STEP_TIMEOUT_MS = 20_000
# Identity-first: the username page, then the password page. A live session may
# skip either or both.
_MAX_FORM_STEPS = 2


class KeycloakBrowserFlowError(Exception):
    """The browser login did not reach the redirect back with a code."""


@dataclass(frozen=True)
class BrowserTokens:
    access_token: str
    id_token: str


@dataclass(frozen=True)
class Authorization:
    """Where an authorize request ended: the redirect back, and the forms the browser was shown."""

    code: str
    forms_shown: tuple[str, ...]


def is_callback(url: str, state: str) -> bool:
    """Whether ``url`` is the authorization callback for the request with ``state``.

    The SPA's origin sees other navigations too (the SPA's own routes), so only a
    navigation carrying this request's ``state`` and a ``code`` or ``error`` counts.
    """
    if not (url == KEYCLOAK_REDIRECT_URI or url.startswith((f"{KEYCLOAK_REDIRECT_URI}/", f"{KEYCLOAK_REDIRECT_URI}?"))):
        return False
    params = parse_qs(urlparse(url).query)
    return params.get("state") == [state] and ("code" in params or "error" in params)


def authorization_code(callbacks: Sequence[str], state: str) -> str:
    """The ``code`` of the one callback for ``state``; anything else is a failed flow."""
    if len(callbacks) != 1:
        raise KeycloakBrowserFlowError(
            f"expected one callback for this request, saw {len(callbacks)}: {list(callbacks)}"
        )
    (callback,) = callbacks
    params = parse_qs(urlparse(callback).query)
    if not is_callback(callback, state) or "code" not in params:
        raise KeycloakBrowserFlowError(f"callback without this request's code: {params.get('error', sorted(params))}")
    (code,) = params["code"]
    return code


class BrowserLogin:
    """Logs fixture users in through Chromium, one browser context per Keycloak session."""

    def __init__(self, keycloak: KeycloakTokenSource) -> None:
        self.keycloak = keycloak

    def authorize(self, context: BrowserContext, user: KeycloakUser, *, client_id: str, scope: str) -> BrowserTokens:
        """Log ``user`` in (or reuse the context's session) and return the tokens."""
        verifier = secrets.token_urlsafe(48)
        authorization = self.authorization(context, user, client_id=client_id, scope=scope, verifier=verifier)
        return self.exchange(context, authorization.code, client_id=client_id, verifier=verifier)

    def authorization(
        self, context: BrowserContext, user: KeycloakUser, *, client_id: str, scope: str, verifier: str
    ) -> Authorization:
        """Run one authorize request in ``context`` and fill in whatever form Keycloak shows."""
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        query = urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "scope": scope,
                "redirect_uri": KEYCLOAK_REDIRECT_URI,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        redirects: list[str] = []

        def _record(request: Request) -> None:
            # Keycloak reaches the SPA's origin by a 302, and Playwright does not
            # route a request a redirect made, so it is read from the request events.
            if request.is_navigation_request() and is_callback(request.url, state):
                redirects.append(request.url)

        page = context.new_page()
        page.on("request", _record)
        forms: list[str] = []
        try:
            page.goto(f"{self.keycloak.issuer}/protocol/openid-connect/auth?{query}")
            for _ in range(_MAX_FORM_STEPS):
                if redirects:
                    break
                field = _form_field(page)
                forms.append(field)
                with page.expect_navigation(timeout=_STEP_TIMEOUT_MS):
                    page.fill(f'input[name="{field}"]', user.username if field == "username" else user.password)
                    page.press(f'input[name="{field}"]', "Enter")
            if not redirects:
                raise KeycloakBrowserFlowError(f"no redirect back after {forms}; the page is at {page.url}")
        finally:
            page.close()

        return Authorization(code=authorization_code(redirects, state), forms_shown=tuple(forms))

    def exchange(self, context: BrowserContext, code: str, *, client_id: str, verifier: str) -> BrowserTokens:
        response = context.request.post(
            f"{self.keycloak.issuer}/protocol/openid-connect/token",
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": KEYCLOAK_REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        if not response.ok:
            raise KeycloakBrowserFlowError(f"token exchange failed: {response.status} {response.text()[:500]}")
        body = response.json()
        return BrowserTokens(access_token=body["access_token"], id_token=body["id_token"])


def _form_field(page: Page) -> str:
    """The one credential field the login page asks for."""
    for field in ("username", "password"):
        if page.locator(f'input[name="{field}"]:visible').count():
            return field
    raise KeycloakBrowserFlowError(f"expected a Keycloak login form at {page.url}")
