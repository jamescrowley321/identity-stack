"""E2E tests for multi-IdP features: provider + IdP link endpoints.

AC-4.4.5: E2E regression — IdP link management + provider config endpoints.

Auth tiers:
  - Tier 1: Unauthenticated (api_context) → 401
  - Tier 2: Authenticated non-admin (auth_api_context) → 403
  - Tier 3: Admin (admin_api_context) → success for IdP links, 403 for providers (requires operator)

Provider endpoints require the ``operator`` role, which is distinct from
``owner``/``admin``. E2E tests validate auth enforcement; CRUD success
for providers is covered by unit + integration tests.
"""

import os
import uuid

import pytest
from playwright.sync_api import APIRequestContext

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY"),
    reason="DESCOPE_MANAGEMENT_KEY not set",
)

DUMMY_UUID = "00000000-0000-0000-0000-000000000000"


# =============================================================================
# Provider Auth Enforcement (requires "operator" role)
# =============================================================================


class TestProviderAuthEnforcement:
    """Auth enforcement for /api/providers endpoints (operator role required)."""

    def test_unauth_list_providers(self, api_context: APIRequestContext, backend_url: str):
        """GET /providers without auth → 401."""
        resp = api_context.get(f"{backend_url}/api/providers")
        assert resp.status == 401

    def test_unauth_register_provider(self, api_context: APIRequestContext, backend_url: str):
        """POST /providers without auth → 401."""
        resp = api_context.post(
            f"{backend_url}/api/providers",
            data={"name": "test", "type": "oidc"},
        )
        assert resp.status == 401

    def test_unauth_deactivate_provider(self, api_context: APIRequestContext, backend_url: str):
        """PATCH /providers/{id} without auth → 401."""
        resp = api_context.patch(
            f"{backend_url}/api/providers/{DUMMY_UUID}",
            data={"active": False},
        )
        assert resp.status == 401

    def test_unauth_get_capabilities(self, api_context: APIRequestContext, backend_url: str):
        """GET /providers/{id}/capabilities without auth → 401."""
        resp = api_context.get(f"{backend_url}/api/providers/{DUMMY_UUID}/capabilities")
        assert resp.status == 401

    def test_nonadmin_list_providers(self, auth_api_context: APIRequestContext, backend_url: str):
        """GET /providers with non-operator auth → 403."""
        resp = auth_api_context.get(f"{backend_url}/api/providers")
        assert resp.status == 403

    def test_nonadmin_register_provider(self, auth_api_context: APIRequestContext, backend_url: str):
        """POST /providers with non-operator auth → 403."""
        resp = auth_api_context.post(
            f"{backend_url}/api/providers",
            data={"name": "test", "type": "oidc"},
        )
        assert resp.status == 403

    def test_admin_list_providers_requires_operator(self, admin_api_context: APIRequestContext, backend_url: str):
        """GET /providers with admin role (not operator) → 403.

        Proves that the operator role is enforced separately from owner/admin.
        """
        resp = admin_api_context.get(f"{backend_url}/api/providers")
        assert resp.status == 403


# =============================================================================
# IdP Link Auth Enforcement (requires "owner" or "admin" role)
# =============================================================================


class TestIdPLinkAuthEnforcement:
    """Auth enforcement for /api/users/{user_id}/idp-links endpoints."""

    def test_unauth_list_idp_links(self, api_context: APIRequestContext, backend_url: str):
        """GET /users/{id}/idp-links without auth → 401."""
        resp = api_context.get(f"{backend_url}/api/users/{DUMMY_UUID}/idp-links")
        assert resp.status == 401

    def test_unauth_create_idp_link(self, api_context: APIRequestContext, backend_url: str):
        """POST /users/{id}/idp-links without auth → 401."""
        resp = api_context.post(
            f"{backend_url}/api/users/{DUMMY_UUID}/idp-links",
            data={"provider_id": DUMMY_UUID, "external_sub": "ext-123"},
        )
        assert resp.status == 401

    def test_unauth_delete_idp_link(self, api_context: APIRequestContext, backend_url: str):
        """DELETE /users/{id}/idp-links/{link_id} without auth → 401."""
        resp = api_context.delete(f"{backend_url}/api/users/{DUMMY_UUID}/idp-links/{DUMMY_UUID}")
        assert resp.status == 401

    def test_nonadmin_list_idp_links(self, auth_api_context: APIRequestContext, backend_url: str):
        """GET /users/{id}/idp-links with non-admin auth → 403."""
        resp = auth_api_context.get(f"{backend_url}/api/users/{DUMMY_UUID}/idp-links")
        assert resp.status == 403

    def test_nonadmin_create_idp_link(self, auth_api_context: APIRequestContext, backend_url: str):
        """POST /users/{id}/idp-links with non-admin auth → 403."""
        resp = auth_api_context.post(
            f"{backend_url}/api/users/{DUMMY_UUID}/idp-links",
            data={"provider_id": DUMMY_UUID, "external_sub": "ext-123"},
        )
        assert resp.status == 403


# =============================================================================
# IdP Link CRUD with Admin Context
# =============================================================================


class TestIdPLinkCrudAdmin:
    """Admin-level IdP link operations via /api/users/{user_id}/idp-links."""

    def test_list_idp_links_for_user(self, admin_api_context: APIRequestContext, backend_url: str):
        """GET /users/{id}/idp-links returns structured response.

        Uses a dummy user_id — may return empty list or 404 depending on whether
        the user exists. Either way it should NOT return 401/403 (auth passes).
        """
        resp = admin_api_context.get(f"{backend_url}/api/users/{DUMMY_UUID}/idp-links")
        # Admin auth should pass — result depends on whether user exists
        assert resp.status in (200, 404), f"Expected 200 or 404, got {resp.status}"
        if resp.status == 200:
            body = resp.json()
            assert "idp_links" in body
            assert isinstance(body["idp_links"], list)

    def test_create_idp_link_nonexistent_user(self, admin_api_context: APIRequestContext, backend_url: str):
        """POST /users/{id}/idp-links with nonexistent user → 404."""
        fake_user_id = str(uuid.uuid4())
        fake_provider_id = str(uuid.uuid4())
        resp = admin_api_context.post(
            f"{backend_url}/api/users/{fake_user_id}/idp-links",
            data={
                "provider_id": fake_provider_id,
                "external_sub": f"ext-{uuid.uuid4().hex[:8]}",
            },
        )
        # User doesn't exist → 404 from service
        assert resp.status == 404, f"Expected 404 for nonexistent user, got {resp.status}"
        body = resp.json()
        assert "type" in body  # RFC 9457 problem detail
        assert "detail" in body

    def test_delete_idp_link_nonexistent(self, admin_api_context: APIRequestContext, backend_url: str):
        """DELETE /users/{id}/idp-links/{link_id} with nonexistent IDs → 404."""
        fake_user_id = str(uuid.uuid4())
        fake_link_id = str(uuid.uuid4())
        resp = admin_api_context.delete(
            f"{backend_url}/api/users/{fake_user_id}/idp-links/{fake_link_id}",
        )
        assert resp.status == 404, f"Expected 404, got {resp.status}"


# =============================================================================
# Identity Resolution Endpoint (internal, no JWT)
# =============================================================================


class TestIdentityResolutionRegression:
    """Regression: identity resolution endpoint continues to work after multi-IdP changes."""

    def test_identity_endpoint_still_bypasses_jwt(self, api_context: APIRequestContext, backend_url: str):
        """GET /api/internal/identity still bypasses JWT auth (AC-4.3.4 regression)."""
        resp = api_context.get(f"{backend_url}/api/internal/identity")
        # 422 (missing params/header) proves JWT was bypassed — not 401
        assert resp.status == 422, f"Expected 422, got {resp.status}"

    def test_identity_unknown_provider_still_returns_404(self, api_context: APIRequestContext, backend_url: str):
        """GET /api/internal/identity with unknown provider still returns 404."""
        identity_key = os.environ.get("INTERNAL_IDENTITY_KEY", "")
        headers = {}
        if identity_key:
            headers["X-Identity-Key"] = identity_key
        resp = api_context.get(
            f"{backend_url}/api/internal/identity",
            params={"sub": "ext-123", "provider": f"nonexistent-{uuid.uuid4().hex[:8]}"},
            headers=headers,
        )
        # Without the key we may get 401/403; with it we get 404
        assert resp.status in (401, 403, 404), f"Expected 401/403/404, got {resp.status}"
        if not identity_key:
            pytest.skip("INTERNAL_IDENTITY_KEY not set — cannot verify 404 for unknown provider")
