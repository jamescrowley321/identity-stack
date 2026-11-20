"""The token the SPA sends for a Descope user names how the user signed in (#699).

The backend takes a Descope token that names no authentication method (``amr``) for
an access key's, a machine's. The SPA signs in through Descope's OIDC authorization
code flow and sends that flow's access token (``useApiClient``), so this signs a
new owner+admin test user in exactly that way: the SPA's Sign In button, Descope's
hosted sign-up-or-in flow with a test OTP, the SPA's code exchange. The access
token it keeps must carry ``amr`` and be served the admin's routes.
"""

import json
import re
import warnings

import pytest
from playwright.sync_api import Browser, expect

from app.dependencies.rbac import MACHINE_PRINCIPAL_REFUSED
from tests.e2e.helpers.auth import ADMIN_ROLES, _decode_jwt_payload, create_test_user, otp_for_test_user
from tests.e2e.not_applicable import descope_credentials_missing

pytestmark = pytest.mark.skipif(
    descope_credentials_missing("DESCOPE_MANAGEMENT_KEY", "E2E_TEST_TENANT_ID", "DESCOPE_PROJECT_ID"),
    reason="DESCOPE_MANAGEMENT_KEY/E2E_TEST_TENANT_ID/DESCOPE_PROJECT_ID not set",
)

_HOSTED_FLOW = re.compile(r"^https://[^/]*descope[^/]*/")
_TIMEOUT_MS = 30_000


def _screen(page) -> str:
    """What the hosted flow shows, for a failure message: its buttons and its inputs, no values."""
    buttons = page.get_by_role("button").all_inner_texts()
    inputs = page.locator("input").evaluate_all("els => els.map(e => [e.type, e.name, e.placeholder])")
    return f"url={page.url.split('?')[0]} buttons={buttons} inputs={inputs}"


@pytest.fixture
def oidc_access_token(browser: Browser, frontend_url: str) -> str:
    """The access token the SPA holds after a test user signs in through Descope's hosted flow."""
    login_id = create_test_user(ADMIN_ROLES)
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    page = context.new_page()
    try:
        page.goto(f"{frontend_url}/login")
        page.get_by_role("button", name="Sign In").click()
        page.wait_for_url(_HOSTED_FLOW, timeout=_TIMEOUT_MS)

        email = page.locator("input[type=email]")
        expect(email, _screen(page)).to_be_visible(timeout=_TIMEOUT_MS)
        email.fill(login_id)
        page.get_by_role("button", name="Continue", exact=True).click()

        code_input = page.locator("input:not([type=email]):visible").first
        expect(code_input, "no code screen after the email").to_be_visible(timeout=_TIMEOUT_MS)
        code = otp_for_test_user(login_id)
        code_input.click()
        page.keyboard.type(code)
        submit = page.get_by_role("button", name=re.compile(r"^(Submit|Continue|Verify)", re.IGNORECASE))
        # The code screen may submit itself once the last digit is in.
        if not page.url.startswith(frontend_url) and submit.count() and submit.first.is_visible():
            submit.first.click()

        page.wait_for_url(re.compile(rf"^{re.escape(frontend_url)}/"), timeout=_TIMEOUT_MS)
        stored = page.wait_for_function(
            """() => Object.keys(sessionStorage).filter(k => k.startsWith("oidc.user:"))
                .map(k => JSON.parse(sessionStorage.getItem(k)).access_token).find(Boolean) || null""",
            timeout=_TIMEOUT_MS,
        ).json_value()
    except Exception as exc:
        raise AssertionError(f"signing in through the hosted flow failed: {_screen(page)}") from exc
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
