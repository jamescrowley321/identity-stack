"""E2E authentication helpers.

Uses the client credentials (access key) flow to get OIDC-compatible tokens.
This produces tokens with the correct issuer for py-identity-model validation.

For test user management, uses the Descope Management API.
"""

import os

import httpx
from py_identity_model import (
    ClientCredentialsTokenRequest,
    DiscoveryDocumentRequest,
    get_discovery_document,
    request_client_credentials_token,
)

DESCOPE_BASE_URL = os.environ.get("DESCOPE_BASE_URL", "https://api.descope.com")
DESCOPE_PROJECT_ID = os.environ.get("DESCOPE_PROJECT_ID", "")
DESCOPE_MANAGEMENT_KEY = os.environ.get("DESCOPE_MANAGEMENT_KEY", "")
DESCOPE_CLIENT_ID = os.environ.get("DESCOPE_CLIENT_ID", "")
DESCOPE_CLIENT_SECRET = os.environ.get("DESCOPE_CLIENT_SECRET", "")

E2E_TEST_EMAIL = "e2e-test@descope-saas-starter.test"
E2E_TEST_TENANT_ID = os.environ.get("E2E_TEST_TENANT_ID", "")


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {DESCOPE_PROJECT_ID}:{DESCOPE_MANAGEMENT_KEY}"}


def _mgmt_url(path: str) -> str:
    return f"{DESCOPE_BASE_URL}{path}"


def get_access_token_via_client_credentials() -> str:
    """Get an OIDC-compatible access token via client credentials flow.

    Uses DESCOPE_CLIENT_ID and DESCOPE_CLIENT_SECRET (access key credentials).
    The resulting token has the correct issuer for py-identity-model validation.
    """
    disco_address = f"{DESCOPE_BASE_URL}/{DESCOPE_PROJECT_ID}/.well-known/openid-configuration"
    disco = get_discovery_document(DiscoveryDocumentRequest(address=disco_address))
    if not disco.is_successful:
        raise RuntimeError(f"Discovery failed: {disco.error}")

    response = request_client_credentials_token(
        ClientCredentialsTokenRequest(
            client_id=DESCOPE_CLIENT_ID,
            client_secret=DESCOPE_CLIENT_SECRET,
            address=disco.token_endpoint,
            scope="openid",
        )
    )
    if not response.is_successful:
        raise RuntimeError(f"Token request failed: {response.error}")

    return response.token["access_token"]


def ensure_test_user(
    email: str = E2E_TEST_EMAIL,
    tenant_id: str = "",
    roles: list[str] | None = None,
) -> dict:
    """Create a test user if it doesn't already exist. Returns the user dict."""
    tenant_id = tenant_id or E2E_TEST_TENANT_ID
    roles = roles or ["admin"]

    with httpx.Client(timeout=30) as client:
        # Try to load the user first
        resp = client.post(
            _mgmt_url("/v1/mgmt/user"),
            headers=_auth_header(),
            json={"loginId": email},
        )
        if resp.status_code == 200 and resp.json().get("user"):
            return resp.json()["user"]

        # Create the user
        tenants = [{"tenantId": tenant_id}]
        if roles:
            tenants[0]["roleNames"] = roles

        resp = client.post(
            _mgmt_url("/v1/mgmt/user/create"),
            headers=_auth_header(),
            json={
                "loginId": email,
                "email": email,
                "name": "E2E Test User",
                "tenants": tenants if tenant_id else [],
                "test": True,
            },
        )
        resp.raise_for_status()
        return resp.json().get("user", {})


def cleanup_test_user(email: str = E2E_TEST_EMAIL) -> None:
    """Delete the test user."""
    with httpx.Client(timeout=30) as client:
        client.post(
            _mgmt_url("/v1/mgmt/user/delete"),
            headers=_auth_header(),
            json={"loginId": email},
        )
