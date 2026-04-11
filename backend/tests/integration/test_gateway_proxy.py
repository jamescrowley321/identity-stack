"""Integration tests for Tyk gateway proxy and header forwarding.

These tests run against a live Tyk gateway (docker compose --profile gateway up).
Skipped automatically when the gateway is not running.

Refs #164 — Story 1.4: Verify Gateway Proxy and Header Forwarding
"""

import json
import os

import httpx
import pytest

TYK_URL = os.environ.get("TYK_URL", "http://localhost:8080")
TYK_API_DEF_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "tyk", "apps", "saas-backend.json")

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def gateway_url():
    """Return gateway URL, skipping tests if gateway is not reachable."""
    try:
        httpx.get(f"{TYK_URL}/api/health", timeout=5.0)
        # Gateway is reachable — even a 401 means it's up
        return TYK_URL
    except (httpx.ConnectError, httpx.TimeoutException):
        pytest.skip("Tyk gateway not running — start with: docker compose --profile gateway up")


@pytest.fixture(scope="module")
def tyk_api_def():
    """Load and return the Tyk API definition, skipping if not found."""
    if not os.path.exists(TYK_API_DEF_PATH):
        pytest.skip("Tyk API definition not found")
    with open(TYK_API_DEF_PATH) as f:
        return json.load(f)


async def test_health_proxy(gateway_url):
    """AC1: Tyk proxies GET /api/health to the backend without requiring auth."""
    async with httpx.AsyncClient(base_url=gateway_url, timeout=10.0) as client:
        response = await client.get("/api/health")
        assert response.status_code == 200, (
            f"Expected 200 for /api/health (auth-exempt ignored path), got {response.status_code}"
        )
        data = response.json()
        assert data["status"] == "ok"


async def test_health_no_auth_via_gateway(gateway_url):
    """AC2: Health endpoint returns 200 even with an invalid token — auth is truly ignored."""
    async with httpx.AsyncClient(base_url=gateway_url, timeout=10.0) as client:
        # Send a garbage Authorization header to prove auth is *ignored*, not just optional.
        # If Tyk were merely allowing unauthenticated requests but still validating tokens
        # when present, this would return 401. The ignored path skips validation entirely.
        response = await client.get(
            "/api/health",
            headers={"Authorization": "Bearer garbage.not.a.jwt"},
        )
        assert response.status_code == 200, (
            f"Expected 200 for /api/health even with invalid token (ignored path), got {response.status_code}"
        )


async def test_invalid_jwt_rejected(gateway_url):
    """AC4: Invalid JWT is rejected by Tyk with 401 before reaching the backend."""
    invalid_jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0IiwiZXhwIjoxMDAwMDAwMDAwfQ.invalid-signature"
    async with httpx.AsyncClient(base_url=gateway_url, timeout=10.0) as client:
        response = await client.get(
            "/api/me",
            headers={"Authorization": f"Bearer {invalid_jwt}"},
        )
        assert response.status_code in (401, 403), f"Expected 401/403 for invalid JWT, got {response.status_code}"


async def test_expired_jwt_rejected(gateway_url):
    """AC4: Expired JWT is rejected by Tyk with 401."""
    # Minimal expired JWT (exp in the past)
    expired_jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0IiwiZXhwIjoxfQ.invalid-signature"
    async with httpx.AsyncClient(base_url=gateway_url, timeout=10.0) as client:
        response = await client.get(
            "/api/me",
            headers={"Authorization": f"Bearer {expired_jwt}"},
        )
        assert response.status_code in (401, 403), f"Expected 401/403 for expired JWT, got {response.status_code}"


async def test_missing_auth_rejected(gateway_url):
    """AC5: No Authorization header to protected endpoint returns 401."""
    async with httpx.AsyncClient(base_url=gateway_url, timeout=10.0) as client:
        response = await client.get("/api/me")
        assert response.status_code in (401, 403), f"Expected 401/403 for missing auth, got {response.status_code}"


async def test_empty_auth_header_rejected(gateway_url):
    """Edge case: Empty Authorization header is rejected."""
    async with httpx.AsyncClient(base_url=gateway_url, timeout=10.0) as client:
        response = await client.get(
            "/api/me",
            headers={"Authorization": ""},
        )
        assert response.status_code in (401, 403), f"Expected 401/403 for empty auth, got {response.status_code}"


async def test_malformed_bearer_rejected(gateway_url):
    """Edge case: Malformed Bearer token is rejected."""
    async with httpx.AsyncClient(base_url=gateway_url, timeout=10.0) as client:
        response = await client.get(
            "/api/me",
            headers={"Authorization": "Bearer not.a.real.token"},
        )
        assert response.status_code in (401, 403), f"Expected 401/403 for malformed bearer, got {response.status_code}"


def test_tyk_config_health_ignored(tyk_api_def):
    """AC1: Tyk config has /api/health in ignored paths to bypass JWT validation."""
    extended_paths = (
        tyk_api_def.get("version_data", {}).get("versions", {}).get("Default", {}).get("extended_paths", {})
    )
    ignored = extended_paths.get("ignored", [])
    health_paths = [entry for entry in ignored if entry.get("path") == "/api/health"]
    assert len(health_paths) == 1, "Expected exactly one ignored entry for /api/health"
    method_actions = health_paths[0].get("method_actions", {})
    assert "GET" in method_actions, "GET method must be in method_actions for /api/health"
    assert method_actions["GET"].get("action") == "no_action"


def test_tyk_config_strip_auth_data(tyk_api_def):
    """AC3: Tyk config has strip_auth_data=false so Authorization header is forwarded.

    Config-based verification: confirms the setting that controls whether Tyk strips
    the Authorization header before proxying. Runtime verification of header passthrough
    requires a valid JWT in the test environment.
    """
    assert tyk_api_def.get("strip_auth_data") is False, (
        "strip_auth_data must be false to forward Authorization header to backend"
    )


def test_tyk_config_preserve_host_header(tyk_api_def):
    """AC2: Tyk config has preserve_host_header=true for proper proxy headers.

    Config-based verification: Tyk automatically sets X-Forwarded-For, X-Forwarded-Proto,
    and X-Real-IP for all proxied requests. This test verifies preserve_host_header is
    enabled. Runtime header inspection would require a debug/echo endpoint.
    """
    proxy = tyk_api_def.get("proxy", {})
    assert proxy.get("preserve_host_header") is True, (
        "proxy.preserve_host_header must be true for proper header forwarding"
    )


def test_tyk_config_openid_enabled(tyk_api_def):
    """AC4/AC5: Tyk config uses OpenID Connect for JWT validation."""
    assert tyk_api_def.get("use_openid") is True, "use_openid must be true"
    providers = tyk_api_def.get("openid_options", {}).get("providers", [])
    assert len(providers) >= 1, "Must have at least one OpenID provider for JWT validation"


def test_tyk_config_listen_path(tyk_api_def):
    """AC1: Tyk listens on /api/ and proxies to backend root.

    With strip_listen_path=false, Tyk appends the full request path (including
    the /api/ prefix) to the target URL. So target_url must be the backend
    root — otherwise /api/foo forwards to /api/api/foo and 404s.
    """
    assert tyk_api_def.get("listen_path") == "/api/"
    assert tyk_api_def.get("proxy", {}).get("target_url") == "http://backend:8000/"
    assert tyk_api_def.get("strip_listen_path") is False
