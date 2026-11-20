"""E2E authentication helpers.

Uses two approaches:
1. Client credentials (access key) for API-only tests — OIDC-compatible tokens
2. Session token injection for browser tests — uses client credentials tokens
   injected into sessionStorage for react-oidc-context to pick up

The test user is created via Management API with `test: True`.
"""

import base64
import json
import os
from http import HTTPStatus

import httpx
from playwright.sync_api import BrowserContext

# Management-API surface and the leak sweeps live in descope_mgmt so they can be
# unit-tested without importing playwright. Re-exported here because conftest and
# the E2E suite import them from this module.
from tests.e2e.helpers.descope_mgmt import (  # noqa: F401 - re-exported for callers
    DESCOPE_BASE_URL,
    DESCOPE_MANAGEMENT_KEY,
    DESCOPE_PROJECT_ID,
    E2E_TEST_EMAIL,
    E2E_TEST_TENANT_ID,
    LEAKED_ACCESS_KEY_PREFIX,
    LEAKED_USER_PREFIXES,
    PERSON_USER_PREFIX,
    _auth_header,
    _mgmt_url,
    sweep_leaked_e2e_access_keys,
    sweep_leaked_e2e_rbac,
    sweep_leaked_e2e_users,
)


def ensure_test_user(
    email: str = "",
    tenant_id: str = "",
    roles: list[str] | None = None,
) -> dict:
    """Create a test user if it doesn't already exist.

    Raises RuntimeError if E2E_TEST_EMAIL is not configured.
    """
    email = email or E2E_TEST_EMAIL
    if not email:
        raise RuntimeError("E2E_TEST_EMAIL must be set — cannot create test user with empty email")
    tenant_id = tenant_id or E2E_TEST_TENANT_ID
    roles = roles or ["owner", "admin"]

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            _mgmt_url("/v1/mgmt/user"),
            headers=_auth_header(),
            json={"loginId": email},
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("user"):
            user = data["user"]
            # Ensure user is in the correct tenant with the right roles
            if tenant_id:
                user_tenants = [t.get("tenantId") for t in user.get("userTenants", [])]
                print(f"[E2E] Existing user tenants: {user_tenants}, need: {tenant_id}")
                if tenant_id not in user_tenants:
                    # Add user to tenant with roles
                    add_resp = client.post(
                        _mgmt_url("/v1/mgmt/user/update/tenant/add"),
                        headers=_auth_header(),
                        json={"loginId": email, "tenantId": tenant_id},
                    )
                    print(f"[E2E] Add user to tenant: {add_resp.status_code} {add_resp.text[:200]}")
                    if roles:
                        role_resp = client.post(
                            _mgmt_url("/v1/mgmt/user/update/role/add"),
                            headers=_auth_header(),
                            json={"loginId": email, "tenantId": tenant_id, "roleNames": roles},
                        )
                        print(f"[E2E] Add roles: {role_resp.status_code} {role_resp.text[:200]}")
            return user

        tenants = [{"tenantId": tenant_id, "roleNames": roles}] if tenant_id else []
        resp = client.post(
            _mgmt_url("/v1/mgmt/user/create"),
            headers=_auth_header(),
            json={
                "loginId": email,
                "email": email,
                "name": "E2E Test User",
                "tenants": tenants,
                "verifiedEmail": True,
                "test": True,
            },
        )
        resp.raise_for_status()
        return resp.json().get("user", {})


def get_oidc_access_token() -> str:
    """Get an OIDC-compatible access token via client credentials flow."""
    from py_identity_model import (
        ClientCredentialsTokenRequest,
        DiscoveryDocumentRequest,
        get_discovery_document,
        request_client_credentials_token,
    )

    client_id = os.environ.get("DESCOPE_CLIENT_ID", "")
    client_secret = os.environ.get("DESCOPE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("DESCOPE_CLIENT_ID/DESCOPE_CLIENT_SECRET not set")

    disco_addr = f"{DESCOPE_BASE_URL}/{DESCOPE_PROJECT_ID}/.well-known/openid-configuration"
    disco = get_discovery_document(DiscoveryDocumentRequest(address=disco_addr))
    if not disco.is_successful:
        raise RuntimeError(f"Discovery failed: {disco.error}")

    response = request_client_credentials_token(
        ClientCredentialsTokenRequest(
            client_id=client_id,
            client_secret=client_secret,
            address=disco.token_endpoint,
            scope="openid",
        )
    )
    if not response.is_successful:
        raise RuntimeError(f"Token request failed: {response.error}")

    return response.token["access_token"]


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload with proper base64 padding handling."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid JWT format: expected 3 parts, got {len(parts)}")
    payload_b64 = parts[1]
    # Add correct padding — base64 requires length to be multiple of 4
    payload_b64 += "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


def create_authenticated_context(browser, frontend_url: str, access_token: str) -> BrowserContext:
    """Create a browser context with OIDC tokens injected into sessionStorage.

    Uses context.add_init_script() to inject the access token as an OIDC user
    object BEFORE the page loads, so react-oidc-context finds an authenticated
    session on first render.
    """
    authority = f"{DESCOPE_BASE_URL}/{DESCOPE_PROJECT_ID}"
    storage_key = f"oidc.user:{authority}:{DESCOPE_PROJECT_ID}"

    payload = _decode_jwt_payload(access_token)

    oidc_user = {
        "id_token": access_token,
        "session_state": None,
        "access_token": access_token,
        "refresh_token": "",
        "token_type": "Bearer",
        "scope": "openid profile email",
        "profile": {
            "sub": payload.get("sub", ""),
        },
        "expires_at": payload.get("exp", 9999999999),
    }

    init_script = f"sessionStorage.setItem({json.dumps(storage_key)}, {json.dumps(json.dumps(oidc_user))});"

    context = browser.new_context(viewport={"width": 1280, "height": 720})
    context.add_init_script(init_script)
    return context


# The roles of the run's two Descope principals in the test tenant: ``ADMIN`` and ``MEMBER``.
ADMIN_ROLES = ("owner", "admin")
MEMBER_ROLES = ("viewer",)


def ensure_person(role_names: tuple[str, ...], tenant_id: str = "") -> str:
    """The Descope test user holding exactly ``role_names`` in the test tenant, created when missing.

    Returns its login id. One user per role set, kept across runs like
    ``E2E_TEST_EMAIL`` and never swept: Descope allows a project five test users.
    One found with any other tenants or roles is deleted and made again, so a
    session's one tenant is the test tenant (its ``dct``) and its roles are these.
    """
    tenant_id = tenant_id or E2E_TEST_TENANT_ID
    if not DESCOPE_MANAGEMENT_KEY:
        raise RuntimeError("DESCOPE_MANAGEMENT_KEY must be set")
    if not tenant_id:
        raise RuntimeError("E2E_TEST_TENANT_ID must be set")
    domain = E2E_TEST_EMAIL.rpartition("@")[2] or "example.com"
    login_id = f"{PERSON_USER_PREFIX}{'-'.join(role_names)}@{domain}"
    wanted = [{"tenantId": tenant_id, "roleNames": sorted(role_names)}]
    with httpx.Client(timeout=30) as client:
        loaded = client.get(_mgmt_url("/v1/mgmt/user"), headers=_auth_header(), params={"loginId": login_id})
        if loaded.status_code == HTTPStatus.OK:
            user = loaded.json().get("user", {})
            held = [
                {"tenantId": t.get("tenantId"), "roleNames": sorted(t.get("roleNames") or [])}
                for t in user.get("userTenants") or []
            ]
            if user.get("test") and held == wanted:
                return login_id
            client.post(_mgmt_url("/v1/mgmt/user/delete"), headers=_auth_header(), json={"loginId": login_id})
        created = client.post(
            _mgmt_url("/v1/mgmt/user/create/test"),
            headers=_auth_header(),
            json={
                "loginId": login_id,
                "email": login_id,
                "name": "E2E Person",
                "verifiedEmail": True,
                "userTenants": [{"tenantId": tenant_id, "roleNames": list(role_names)}],
            },
        )
    if created.status_code != HTTPStatus.OK:
        raise RuntimeError(f"Creating a test user failed: HTTP {created.status_code} {created.text[:200]}")
    return login_id


def otp_for_test_user(login_id: str) -> str:
    """A one-time code a test user signs in with, by email OTP (Descope sends no message to a test user)."""
    with httpx.Client(timeout=30) as client:
        otp = client.post(
            _mgmt_url("/v1/mgmt/tests/generate/otp"),
            headers=_auth_header(),
            json={"loginId": login_id, "deliveryMethod": "email"},
        )
    if otp.status_code != HTTPStatus.OK:
        raise RuntimeError(f"Generating a test user's OTP failed: HTTP {otp.status_code} {otp.text[:200]}")
    code = otp.json().get("code", "")
    if not code:
        raise RuntimeError("Generating a test user's OTP returned no code")
    return code


def enchanted_link_for_test_user(login_id: str) -> str:
    """An enchanted link a test user signs in with (Descope sends no message to a test user)."""
    with httpx.Client(timeout=30) as client:
        generated = client.post(
            _mgmt_url("/v1/mgmt/tests/generate/enchantedlink"),
            headers=_auth_header(),
            json={"loginId": login_id, "URI": ""},
        )
    if generated.status_code != HTTPStatus.OK:
        raise RuntimeError(
            f"Generating a test user's enchanted link failed: HTTP {generated.status_code} {generated.text[:200]}"
        )
    link = generated.json().get("link", "")
    if not link:
        raise RuntimeError("Generating a test user's enchanted link returned no link")
    return link


def verify_enchanted_link(link: str) -> int:
    """Verify an enchanted link's token at Descope, as clicking it does. Returns the HTTP status."""
    from urllib.parse import parse_qs, urlsplit

    token = parse_qs(urlsplit(link).query).get("t", [""])[0]
    with httpx.Client(timeout=30) as client:
        verified = client.post(
            f"{DESCOPE_BASE_URL}/v1/auth/enchantedlink/verify",
            headers={"Authorization": f"Bearer {DESCOPE_PROJECT_ID}"},
            json={"token": token},
        )
    return verified.status_code


def get_user_session_token(role_names: tuple[str, ...]) -> str:
    """A Descope USER session of the test user holding ``role_names`` in the test tenant (``ensure_person``).

    The run's ``ADMIN`` (``ADMIN_ROLES``) and ``MEMBER`` (``MEMBER_ROLES``) are people:
    an access-key session is a machine, which the backend refuses on ``/api/keys``
    and the role and member routes before any role is read (#699).
    """
    login_id = ensure_person(role_names)
    code = otp_for_test_user(login_id)
    with httpx.Client(timeout=30) as client:
        verified = client.post(
            f"{DESCOPE_BASE_URL}/v1/auth/otp/verify/email",
            headers={"Authorization": f"Bearer {DESCOPE_PROJECT_ID}"},
            json={"loginId": login_id, "code": code},
        )
    if verified.status_code != HTTPStatus.OK:
        raise RuntimeError(f"Verifying a test user's OTP failed: HTTP {verified.status_code} {verified.text[:200]}")
    token = verified.json().get("sessionJwt", "")
    if not token:
        raise RuntimeError("Verifying a test user's OTP returned no sessionJwt")
    return token


def get_access_key_session_token(tenant_id: str = "") -> str:
    """A Descope session exchanged for a new owner+admin access key of the test tenant: a machine.

    Strategy: create a temporary tenant-scoped access key with admin/owner roles
    via the Management API, then exchange it via Descope's access key exchange
    endpoint. The resulting session JWT is signed by Descope's project keys and
    carries the tenants claim require_role() reads, and no ``amr``: the backend
    takes it for a machine (#699).
    """
    import uuid

    tenant_id = tenant_id or E2E_TEST_TENANT_ID
    if not DESCOPE_MANAGEMENT_KEY:
        raise RuntimeError("DESCOPE_MANAGEMENT_KEY must be set")
    if not tenant_id:
        raise RuntimeError("E2E_TEST_TENANT_ID must be set")

    # Step 1: Create a temporary tenant-scoped access key with admin roles.
    # Must use keyTenants array format (not top-level tenantId) so the
    # exchanged session JWT includes dct and tenants claims.
    key_name = f"e2e-admin-{uuid.uuid4().hex[:8]}"
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            _mgmt_url("/v1/mgmt/accesskey/create"),
            headers=_auth_header(),
            json={
                "name": key_name,
                "keyTenants": [
                    {"tenantId": tenant_id, "roleNames": ["owner", "admin"]},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        key_id = data.get("key", {}).get("id", "")
        cleartext = data.get("cleartext", "")
        if not cleartext:
            raise RuntimeError(f"Access key creation returned no cleartext: {data}")

    # Step 2: Exchange access key for a Descope session JWT via auth endpoint.
    # Unlike the OIDC client credentials flow, the exchange endpoint returns a
    # session JWT that includes Descope-specific claims (dct, tenants, roles).
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{DESCOPE_BASE_URL}/v1/auth/accesskey/exchange",
                headers={"Authorization": f"Bearer {DESCOPE_PROJECT_ID}:{cleartext}"},
                json={},
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("sessionJwt", "")
            if not token:
                raise RuntimeError(f"Access key exchange returned no sessionJwt: {data}")
    finally:
        # Step 3: delete the temporary access key.
        #
        # This runs in a `finally`, so it must not raise: doing so would replace
        # whatever real failure brought us here. But it must not be silent
        # either — the key carries owner+admin on the shared project and has no
        # expiry, so a swallowed failure leaks a standing privileged credential,
        # one per session. Report it loudly and leave the session sweep to
        # collect it on the next run.
        try:
            with httpx.Client(timeout=10) as client:
                delete_resp = client.post(
                    _mgmt_url("/v1/mgmt/accesskey/delete"),
                    headers=_auth_header(),
                    json={"id": key_id},
                )
            if delete_resp.status_code != HTTPStatus.OK:
                print(
                    f"[E2E] LEAK: failed to delete admin access key {key_name} "
                    f"({key_id}): HTTP {delete_resp.status_code}. It carries owner+admin "
                    "and never expires; the next run's sweep should remove it."
                )
        except Exception as exc:  # noqa: BLE001 - never mask the original failure
            print(f"[E2E] LEAK: error deleting admin access key {key_name} ({key_id}): {exc!r}")

    return token


def cleanup_test_user(email: str = "") -> None:
    """Delete the test user."""
    email = email or E2E_TEST_EMAIL
    with httpx.Client(timeout=30) as client:
        client.post(
            _mgmt_url("/v1/mgmt/user/delete"),
            headers=_auth_header(),
            json={"loginId": email},
        )
