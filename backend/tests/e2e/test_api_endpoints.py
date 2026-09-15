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
            ("GET", "/api/providers"),
            ("GET", "/api/sync/status"),
            ("GET", "/api/events/recent"),
            ("GET", "/api/users"),
        ],
    )
    def test_protected_endpoints_return_401(
        self, api_context: APIRequestContext, backend_url: str, method: str, path: str
    ):
        """All protected endpoints return 401 without an auth token."""
        resp = api_context.get(f"{backend_url}{path}")
        assert resp.status == 401, f"{method} {path} returned {resp.status}, expected 401"

    @pytest.mark.parametrize(
        "method,path",
        [
            ("POST", "/api/providers"),
            ("GET", "/api/users/00000000-0000-0000-0000-000000000000/idp-links"),
            ("POST", "/api/users/00000000-0000-0000-0000-000000000000/idp-links"),
        ],
    )
    def test_new_write_endpoints_return_401(
        self, api_context: APIRequestContext, backend_url: str, method: str, path: str
    ):
        """New write endpoints also require auth."""
        if method == "POST":
            resp = api_context.post(f"{backend_url}{path}", data="{}")
        else:
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
        """Roles endpoint returns 200 for an owner+admin token.

        Exactly 200. The admin fixture holds owner and admin and the route gates
        on `require_role("owner", "admin")`, so 403 would be a regression in the
        fixture or the gate; 502 would be a Descope outage. Accepting either made
        this pass while proving nothing.
        """
        resp = admin_api_context.get(f"{backend_url}/api/roles")
        assert resp.status == 200, f"/api/roles returned {resp.status}: {resp.text()[:200]}"
        assert isinstance(resp.json().get("roles"), list)

    def test_permissions_list_responds(self, admin_api_context: APIRequestContext, backend_url: str):
        """Exactly 200 — see test_roles_list_responds."""
        resp = admin_api_context.get(f"{backend_url}/api/permissions")
        assert resp.status == 200, f"/api/permissions returned {resp.status}: {resp.text()[:200]}"
        assert isinstance(resp.json().get("permissions"), list)

    def test_members_list_responds(self, admin_api_context: APIRequestContext, backend_url: str):
        """Exactly 200 — see test_roles_list_responds."""
        resp = admin_api_context.get(f"{backend_url}/api/members")
        assert resp.status == 200, f"/api/members returned {resp.status}: {resp.text()[:200]}"
        assert isinstance(resp.json().get("members"), list)

    def test_keys_list_responds(self, admin_api_context: APIRequestContext, backend_url: str):
        """Exactly 200 — see test_roles_list_responds."""
        resp = admin_api_context.get(f"{backend_url}/api/keys")
        assert resp.status == 200, f"/api/keys returned {resp.status}: {resp.text()[:200]}"
        assert isinstance(resp.json().get("keys"), list)

    def test_providers_list_requires_operator(self, admin_api_context: APIRequestContext, backend_url: str):
        """403 for an owner+admin token: this route gates on `operator`, which it lacks.

        Deterministic, so asserted exactly. `in (200, 403)` covered both "the gate
        works" and "the gate is gone" with one assertion.
        """
        resp = admin_api_context.get(f"{backend_url}/api/providers")
        assert resp.status == 403, f"/api/providers returned {resp.status}: {resp.text()[:200]}"

    def test_idp_links_responds(self, admin_api_context: APIRequestContext, backend_url: str):
        """IdP links endpoint responds (200/403/404 depending on user existence)."""
        fake_user = "00000000-0000-0000-0000-000000000000"
        resp = admin_api_context.get(f"{backend_url}/api/users/{fake_user}/idp-links")
        # Exactly 404. This user id deliberately does not exist, so 200 would mean
        # the tenant guard resolved a user it should not have — cross-tenant
        # disclosure passing as a green test. 404 rather than 403 is deliberate:
        # it does not leak whether the user exists.
        assert resp.status == 404, f"/api/users/{{id}}/idp-links returned {resp.status}: {resp.text()[:200]}"
        problem = resp.json()
        assert problem.get("status") == 404, f"not an RFC 9457 problem detail: {problem}"
        assert problem.get("title"), f"problem detail has no title: {problem}"

    def test_sync_status_responds(self, admin_api_context: APIRequestContext, backend_url: str):
        """Sync status endpoint responds (200 with operator role, 403 otherwise)."""
        resp = admin_api_context.get(f"{backend_url}/api/sync/status")
        # Gated on `operator`, which the admin fixture lacks. Deterministic 403.
        assert resp.status == 403, f"/api/sync/status returned {resp.status}: {resp.text()[:200]}"

    def test_events_recent_responds(self, admin_api_context: APIRequestContext, backend_url: str):
        """Recent events endpoint responds (200 with operator role, 403 otherwise)."""
        resp = admin_api_context.get(f"{backend_url}/api/events/recent")
        # Gated on `operator`, which the admin fixture lacks. Deterministic 403.
        assert resp.status == 403, f"/api/events/recent returned {resp.status}: {resp.text()[:200]}"

    def test_events_recent_rejects_invalid_limit(self, admin_api_context: APIRequestContext, backend_url: str):
        """Limit out of range is refused — either by the operator gate or by validation.

        Left permissive deliberately, unlike the assertions above. FastAPI solves
        dependencies and validates query parameters in the same pass, so whether
        `require_role("operator")` raises 403 before the `limit` bound produces
        422 is an ordering detail of the framework, not a property of this API.
        Both are a refusal; neither is 200.

        The consequence worth naming: with only an owner+admin fixture available,
        the 422 path is never reached, so the bound itself is effectively
        untested. Fixing that needs an operator-roled fixture, which does not
        exist yet.
        """
        for query in ("limit=0", "limit=201"):
            resp = admin_api_context.get(f"{backend_url}/api/events/recent?{query}")
            assert resp.status in (403, 422), f"/api/events/recent?{query} returned {resp.status}"

    def test_canonical_users_responds(self, admin_api_context: APIRequestContext, backend_url: str):
        """Canonical users endpoint responds (200 with operator role, 403 otherwise)."""
        resp = admin_api_context.get(f"{backend_url}/api/users")
        # Gated on `operator`, which the admin fixture lacks. Deterministic 403.
        assert resp.status == 403, f"/api/users returned {resp.status}: {resp.text()[:200]}"

    def test_canonical_users_rejects_invalid_status(self, admin_api_context: APIRequestContext, backend_url: str):
        """Unknown status string is refused — see test_events_recent_rejects_invalid_limit."""
        resp = admin_api_context.get(f"{backend_url}/api/users?status=bogus")
        assert resp.status in (403, 422), f"/api/users?status=bogus returned {resp.status}"

    def test_admin_endpoints_reject_non_admin_token(self, auth_api_context: APIRequestContext, backend_url: str):
        """Admin endpoints return 403 with a valid but non-admin token."""
        admin_only = [
            "/api/roles",
            "/api/permissions",
            "/api/members",
            "/api/keys",
            "/api/sync/status",
            "/api/events/recent",
            "/api/users",
        ]
        for path in admin_only:
            resp = auth_api_context.get(f"{backend_url}{path}")
            # Exactly 403. Accepting 200 made this a security test that could not
            # fail: a regression opening the entire admin surface to a non-admin
            # token was as green as the gate working. 502 is also out — an
            # authorization denial happens in the dependency, before any upstream
            # call, so it cannot legitimately be an upstream error.
            assert resp.status == 403, (
                f"{path} returned {resp.status} to a non-admin token, expected 403: {resp.text()[:200]}"
            )
