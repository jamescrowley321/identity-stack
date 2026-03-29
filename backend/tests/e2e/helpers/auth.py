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

import httpx
from playwright.sync_api import BrowserContext

DESCOPE_BASE_URL = os.environ.get("DESCOPE_BASE_URL", "https://api.descope.com")
DESCOPE_PROJECT_ID = os.environ.get("DESCOPE_PROJECT_ID", "")
DESCOPE_MANAGEMENT_KEY = os.environ.get("DESCOPE_MANAGEMENT_KEY", "")

E2E_TEST_EMAIL = os.environ.get("E2E_TEST_EMAIL", "")
E2E_TEST_TENANT_ID = os.environ.get("E2E_TEST_TENANT_ID", "")


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {DESCOPE_PROJECT_ID}:{DESCOPE_MANAGEMENT_KEY}"}


def _mgmt_url(path: str) -> str:
    return f"{DESCOPE_BASE_URL}{path}"


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
            return data["user"]

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


def get_admin_session_token(email: str = "", tenant_id: str = "") -> str:
    """Get an OIDC-compatible token with admin/owner roles for E2E testing.

    Strategy: get a standard OIDC client credentials token (passes OIDC validation),
    then enrich it with tenant/role claims via the Descope Management API's
    jwt/update endpoint. The result is a token that:
    1. Passes OIDC validation (correct issuer, audience, JWKS signature)
    2. Contains dct and tenants claims needed by require_role()
    """
    tenant_id = tenant_id or E2E_TEST_TENANT_ID
    if not DESCOPE_MANAGEMENT_KEY:
        raise RuntimeError("DESCOPE_MANAGEMENT_KEY must be set")
    if not tenant_id:
        raise RuntimeError("E2E_TEST_TENANT_ID must be set")

    # Step 1: Get a standard OIDC client credentials token
    base_token = get_oidc_access_token()

    # Step 2: Enrich the token with tenant/role claims via Management API
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            _mgmt_url("/v1/mgmt/jwt/update"),
            headers=_auth_header(),
            json={
                "jwt": base_token,
                "customClaims": {
                    "dct": tenant_id,
                    "tenants": {
                        tenant_id: {
                            "roles": ["owner", "admin"],
                        },
                    },
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("jwt", "")
        if not token:
            raise RuntimeError(f"JWT update returned no token: {data}")

    # Debug: log decoded claims for CI diagnosis
    try:
        payload = _decode_jwt_payload(token)
        tenants_info = payload.get("tenants")
        if isinstance(tenants_info, dict):
            tenants_info = list(tenants_info.keys())
        print(
            f"[E2E] Admin token claims: dct={payload.get('dct')}, "
            f"tenants={tenants_info}, "
            f"iss={payload.get('iss')}, aud={payload.get('aud')}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[E2E] Could not decode admin token: {exc}")

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
