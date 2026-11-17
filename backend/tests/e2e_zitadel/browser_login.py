"""Zitadel logins driven through Chromium and Login V2.

Two kinds. ``sign_in`` fills in Login V2's forms on whatever page the SPA's own
Sign In led to. ``BrowserLogin.authorize`` runs the authorization-code flow with
PKCE S256 for a client the SPA does not use (the project's second
application): through the same forms, up to the redirect back to the SPA's
origin, whose ``code`` is exchanged at the token endpoint. The SPA page that
redirect lands on finds no sign-in of its own for the ``state`` and exchanges
nothing.

Login V2 asks for the login name on one page and the password on the next. A
live Zitadel session in the browser context may skip either or both.
"""

import base64
import hashlib
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import BrowserContext, Page, Request

# A form step that does not leave the page within this long has failed.
_STEP_TIMEOUT_MS = 20_000
# The login name page, then the password page.
_MAX_FORM_STEPS = 2

LOGIN_NAME_FIELD = '[data-testid="username-text-input"]'
PASSWORD_FIELD = '[data-testid="password-text-input"]'
SUBMIT = '[data-testid="submit-button"]'
# Login V2's login name and password pages. The authorize request reaches them
# through a redirect or two, and a form filled before the last one is lost.
_LOGIN_FORM_PAGE = re.compile(r"/ui/v2/login/(loginname|password)\?")


class ZitadelBrowserFlowError(Exception):
    """The browser login did not reach the redirect back with a code."""


@dataclass(frozen=True)
class ZitadelUser:
    login_name: str
    password: str


@dataclass(frozen=True)
class BrowserTokens:
    access_token: str
    id_token: str


def fill_login_form(page: Page, user: ZitadelUser) -> str:
    """Fill in the one Login V2 form the page shows and submit it; return which it was.

    Login V2 renders its forms on the server and wires them up in the browser:
    a value typed before the page is idle is not seen and the button stays
    disabled, so the form is filled once the network is idle.
    """
    page.wait_for_url(_LOGIN_FORM_PAGE, timeout=_STEP_TIMEOUT_MS)
    page.wait_for_load_state("networkidle")
    if page.locator(LOGIN_NAME_FIELD).is_visible():
        field, value = LOGIN_NAME_FIELD, user.login_name
    elif page.locator(PASSWORD_FIELD).is_visible():
        field, value = PASSWORD_FIELD, user.password
    else:
        raise ZitadelBrowserFlowError(f"expected a Login V2 form at {page.url}")
    page.locator(field).fill(value)
    with page.expect_navigation(timeout=_STEP_TIMEOUT_MS):
        page.locator(SUBMIT).click()
    return "login name" if field == LOGIN_NAME_FIELD else "password"


def sign_in(page: Page, user: ZitadelUser, frontend_url: str) -> None:
    """After the SPA's Sign In, fill in Login V2's forms until the browser is back on the SPA."""

    def _on_a_form_or_back(url: str) -> bool:
        return url.startswith(frontend_url) and "/login" not in url or bool(_LOGIN_FORM_PAGE.search(url))

    page.wait_for_url(_LOGIN_FORM_PAGE, timeout=_STEP_TIMEOUT_MS)
    for _ in range(_MAX_FORM_STEPS):
        fill_login_form(page, user)
        page.wait_for_url(_on_a_form_or_back, timeout=_STEP_TIMEOUT_MS)
        if page.url.startswith(frontend_url):
            break
    page.wait_for_url(f"{frontend_url}/", timeout=_STEP_TIMEOUT_MS)


def is_callback(url: str, redirect_uri: str, state: str) -> bool:
    """Whether ``url`` is the authorization callback for the request with ``state``."""
    if not (url == redirect_uri or url.startswith((f"{redirect_uri}/", f"{redirect_uri}?"))):
        return False
    params = parse_qs(urlparse(url).query)
    return params.get("state") == [state] and ("code" in params or "error" in params)


def authorization_code(callbacks: Sequence[str], redirect_uri: str, state: str) -> str:
    """The ``code`` of the one callback for ``state``; anything else is a failed flow."""
    if len(callbacks) != 1:
        raise ZitadelBrowserFlowError(f"expected one callback for this request, saw {len(callbacks)}")
    (callback,) = callbacks
    params = parse_qs(urlparse(callback).query)
    if not is_callback(callback, redirect_uri, state) or "code" not in params:
        raise ZitadelBrowserFlowError(f"callback without this request's code: {params.get('error', sorted(params))}")
    (code,) = params["code"]
    return code


class BrowserLogin:
    """Authorization-code logins for a client of the instance, one browser context per Zitadel session."""

    def __init__(self, issuer: str, redirect_uri: str) -> None:
        self.issuer = issuer
        self.redirect_uri = redirect_uri

    def authorize(self, context: BrowserContext, user: ZitadelUser, *, client_id: str, scope: str) -> BrowserTokens:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        query = urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "scope": scope,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        callbacks: list[str] = []

        def _record(request: Request) -> None:
            # Zitadel reaches the SPA's origin by a redirect, which Playwright
            # does not route, so it is read from the request events.
            if request.is_navigation_request() and is_callback(request.url, self.redirect_uri, state):
                callbacks.append(request.url)

        page = context.new_page()
        page.on("request", _record)
        forms: list[str] = []
        try:
            page.goto(f"{self.issuer}/oauth/v2/authorize?{query}")
            for _ in range(_MAX_FORM_STEPS):
                if callbacks:
                    break
                forms.append(fill_login_form(page, user))
            if not callbacks:
                raise ZitadelBrowserFlowError(f"no redirect back after {forms}; the page is at {page.url}")
        finally:
            page.close()

        code = authorization_code(callbacks, self.redirect_uri, state)
        response = context.request.post(
            f"{self.issuer}/oauth/v2/token",
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        if not response.ok:
            raise ZitadelBrowserFlowError(f"token exchange failed: {response.status} {response.text()[:500]}")
        body = response.json()
        return BrowserTokens(access_token=body["access_token"], id_token=body["id_token"])
