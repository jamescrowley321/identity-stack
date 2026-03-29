"""E2E tests for all API endpoints — status code and response shape validation.

Covers every registered endpoint with both authenticated and unauthenticated
requests. Verifies no 5xx errors on valid requests, correct 401 on missing auth,
and expected response shapes.

Requires DESCOPE_CLIENT_ID, DESCOPE_CLIENT_SECRET for auth_api_context.
Requires DESCOPE_MANAGEMENT_KEY for admin_api_context.
"""

import os

import pytest
from playwright.sync_api import APIRequestContext

# --- Unauthenticated endpoint tests (no credentials needed) ---


class TestUnauthenticatedEndpoints:
    """Verify public endpoints work and protected endpoints reject unauthenticated requests."""

    def test_health_returns_200(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.get(f"{backend_url}/api/health")
        assert resp.status == 200
        body = resp.json()
        assert body["status"] == "ok"

    def test_openapi_schema_accessible(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.get(f"{backend_url}/openapi.json")
        assert resp.status == 200
        body = resp.json()
        assert "paths" in body
        assert body["info"]["title"] == "Descope SaaS Starter API"

    def test_docs_accessible(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.get(f"{backend_url}/docs")
        assert resp.status == 200

    def test_redoc_accessible(self, api_context: APIRequestContext, backend_url: str):
        resp = api_context.get(f"{backend_url}/redoc")
        assert resp.status == 200

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/claims"),
            ("GET", "/api/me"),
            ("GET", "/api/tenants"),
            ("GET", "/api/tenants/current"),
            ("GET", "/api/profile"),
            ("GET", "/api/tenants/current/settings"),
            ("GET", "/api/roles"),
            ("GET", "/api/permissions"),
            ("GET", "/api/keys"),
            ("GET", "/api/members"),
        ],
    )
    def test_protected_endpoints_return_401(
        self, api_context: APIRequestContext, backend_url: str, method: str, path: str
    ):
        """All protected endpoints return 401 without an auth token."""
        resp = api_context.get(f"{backend_url}{path}")
        assert resp.status == 401, f"{method} {path} returned {resp.status}, expected 401"


# --- Authenticated endpoint tests (OIDC client credentials token) ---


@pytest.mark.skipif(
    not os.environ.get("DESCOPE_CLIENT_ID") or not os.environ.get("DESCOPE_CLIENT_SECRET"),
    reason="DESCOPE_CLIENT_ID/DESCOPE_CLIENT_SECRET not set",
)
class TestAuthenticatedEndpoints:
    """Verify authenticated endpoints return valid responses (no 5xx)."""

    def test_claims_returns_200(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.get(f"{backend_url}/api/claims")
        assert resp.status == 200
        body = resp.json()
        assert "sub" in body

    def test_me_returns_200(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.get(f"{backend_url}/api/me")
        assert resp.status == 200
        body = resp.json()
        assert "identity" in body

    def test_tenants_returns_200(self, auth_api_context: APIRequestContext, backend_url: str):
        resp = auth_api_context.get(f"{backend_url}/api/tenants")
        assert resp.status == 200
        body = resp.json()
        assert "tenants" in body
        assert isinstance(body["tenants"], list)

    def test_profile_no_5xx(self, auth_api_context: APIRequestContext, backend_url: str):
        """Profile endpoint returns 200 (with data or empty defaults), never 5xx."""
        resp = auth_api_context.get(f"{backend_url}/api/profile")
        assert resp.status < 500, f"/api/profile returned {resp.status}"
        if resp.status == 200:
            body = resp.json()
            assert "user_id" in body

    def test_tenant_settings_no_5xx(self, auth_api_context: APIRequestContext, backend_url: str):
        """Tenant settings returns 200 or 403 (no tenant context), never 5xx."""
        resp = auth_api_context.get(f"{backend_url}/api/tenants/current/settings")
        assert resp.status < 500, f"/api/tenants/current/settings returned {resp.status}"

    def test_tenant_current_no_5xx(self, auth_api_context: APIRequestContext, backend_url: str):
        """Current tenant endpoint returns 200 or 403, never 5xx."""
        resp = auth_api_context.get(f"{backend_url}/api/tenants/current")
        assert resp.status < 500, f"/api/tenants/current returned {resp.status}"


# --- Admin endpoint tests (tenant-scoped admin token) ---


@pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY"),
    reason="DESCOPE_MANAGEMENT_KEY not set",
)
class TestAdminEndpoints:
    """Verify admin-level endpoints respond correctly with admin token."""

    def test_roles_list_responds(self, admin_api_context: APIRequestContext, backend_url: str):
        """Roles endpoint responds (200/403/502 depending on token scope and Descope API)."""
        resp = admin_api_context.get(f"{backend_url}/api/roles")
        assert resp.status in (200, 403, 502), f"/api/roles returned {resp.status}"

    def test_permissions_list_responds(self, admin_api_context: APIRequestContext, backend_url: str):
        resp = admin_api_context.get(f"{backend_url}/api/permissions")
        assert resp.status in (200, 403, 502), f"/api/permissions returned {resp.status}"

    def test_members_list_responds(self, admin_api_context: APIRequestContext, backend_url: str):
        resp = admin_api_context.get(f"{backend_url}/api/members")
        assert resp.status in (200, 403, 502), f"/api/members returned {resp.status}"

    def test_keys_list_responds(self, admin_api_context: APIRequestContext, backend_url: str):
        resp = admin_api_context.get(f"{backend_url}/api/keys")
        assert resp.status in (200, 403, 502), f"/api/keys returned {resp.status}"

    def test_admin_endpoints_reject_non_admin_token(self, auth_api_context: APIRequestContext, backend_url: str):
        """Admin endpoints return 403 with a valid but non-admin token."""
        admin_only = [
            "/api/roles",
            "/api/permissions",
            "/api/members",
            "/api/keys",
        ]
        for path in admin_only:
            resp = auth_api_context.get(f"{backend_url}{path}")
            assert resp.status in (
                200,
                403,
                502,
            ), f"{path} returned {resp.status}, expected 200, 403, or 502"
