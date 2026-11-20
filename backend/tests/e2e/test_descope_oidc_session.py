"""The token the SPA sends for a Descope user names how the user signed in (#699).

The backend takes a Descope token that names no authentication method (``amr``) for
an access key's, a machine's. The SPA signs in through Descope's OIDC authorization
code flow and sends that flow's access token (``useApiClient``), so this signs a
new owner+admin test user in exactly that way: the SPA's Sign In button, Descope's
hosted sign-up-or-in flow with a test user's enchanted link, the SPA's code exchange. The access
token it keeps must carry ``amr`` and be served the admin's routes.
"""

import json
import re
import warnings

import pytest
from playwright.sync_api import Browser, expect
from playwright.sync_api import Error as PlaywrightError

from app.dependencies.rbac import MACHINE_PRINCIPAL_REFUSED
from tests.e2e.helpers.auth import (
    ADMIN_ROLES,
    _decode_jwt_payload,
    enchanted_link_for_test_user,
    ensure_person,
    verify_enchanted_link,
)
from tests.e2e.not_applicable import descope_credentials_missing

pytestmark = pytest.mark.skipif(
    descope_credentials_missing("DESCOPE_MANAGEMENT_KEY", "E2E_TEST_TENANT_ID", "DESCOPE_PROJECT_ID"),
    reason="DESCOPE_MANAGEMENT_KEY/E2E_TEST_TENANT_ID/DESCOPE_PROJECT_ID not set",
)

_HOSTED_FLOW = re.compile(r"^https://[^/]*descope[^/]*/")
_TIMEOUT_MS = 30_000


def _back_at_the_spa(frontend_url: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(frontend_url)}/")


def _screen(page) -> str:
    """What the hosted flow shows, for a failure message: its text, buttons and inputs, no input values."""
    try:
        text = re.sub(r"\s+", " ", page.locator("body").inner_text())[:300]
        buttons = page.get_by_role("button").all_inner_texts()
        inputs = page.locator("input").evaluate_all("els => els.map(e => [e.type, e.name, e.placeholder])")
    except PlaywrightError as exc:
        return f"url={page.url.split('?')[0]} (unreadable: {exc!r})"
    return f"url={page.url.split('?')[0]} text={text!r} buttons={buttons} inputs={inputs}"


@pytest.fixture(scope="module")
def oidc_access_token(browser: Browser, frontend_url: str) -> str:
    """The access token the SPA holds after a test user signs in through Descope's hosted flow."""
    login_id = ensure_person(ADMIN_ROLES)
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    page = context.new_page()
    progress = []
    try:
        page.goto(f"{frontend_url}/login")
        page.get_by_role("button", name="Sign In").click()
        page.wait_for_url(_HOSTED_FLOW, timeout=_TIMEOUT_MS)

        # The project's sign-up-or-in flow: an email, then an enchanted link the page waits on.
        email = page.locator("input[type=email]")
        expect(email, _screen(page)).to_be_visible(timeout=_TIMEOUT_MS)
        email.fill(login_id)
        page.locator("descope-button").filter(has_text=re.compile(r"^\s*Continue\s*$")).click()
        expect(page.get_by_text("verification link"), "no link screen after the email").to_be_visible(
            timeout=_TIMEOUT_MS
        )
        progress.append("link screen")

        link = enchanted_link_for_test_user(login_id)
        progress.append(f"link host {link.split('/')[2] if '://' in link else '(none)'}")
        progress.append(f"verify {verify_enchanted_link(link)}")
        try:
            page.wait_for_url(_back_at_the_spa(frontend_url), timeout=_TIMEOUT_MS)
        except PlaywrightError:
            # Not completed by the verification alone: open the link, as a click in the email would.
            progress.append("opening the link")
            context.new_page().goto(link)
            page.wait_for_url(_back_at_the_spa(frontend_url), timeout=_TIMEOUT_MS)
        progress.append("back at the SPA")

        stored = page.wait_for_function(
            """() => Object.keys(sessionStorage).filter(k => k.startsWith("oidc.user:"))
                .map(k => JSON.parse(sessionStorage.getItem(k)).access_token).find(Boolean) || null""",
            timeout=_TIMEOUT_MS,
        ).json_value()
    except Exception as exc:
        raise AssertionError(f"signing in through the hosted flow failed after {progress}: {_screen(page)}") from exc
    finally:
        context.close()
    assert isinstance(stored, str) and stored, "the SPA holds no access token after sign-in"
    return stored


def test_the_spas_access_token_names_how_the_user_signed_in(oidc_access_token: str):
    claims = _decode_jwt_payload(oidc_access_token)
    # Claim names only, never values: this is the evidence the classifier rests on.
    warnings.warn(f"Descope OIDC access token claim names: {json.dumps(sorted(claims))}", stacklevel=1)

    assert isinstance(claims.get("amr"), list) and claims["amr"], sorted(claims)


def test_the_spas_access_token_is_served_the_admin_routes(playwright, backend_url: str, oidc_access_token: str):
    api = playwright.request.new_context(
        base_url=backend_url, extra_http_headers={"Authorization": f"Bearer {oidc_access_token}"}
    )
    try:
        answers = {path: api.get(path) for path in ("/api/members", "/api/keys")}
        statuses = {
            path: (response.status, None if response.status == 200 else response.json().get("detail"))
            for path, response in answers.items()
        }
    finally:
        api.dispose()

    assert statuses == {"/api/members": (200, None), "/api/keys": (200, None)}, (
        f"{statuses}; {MACHINE_PRINCIPAL_REFUSED!r} means the token was taken for an access key's"
    )
