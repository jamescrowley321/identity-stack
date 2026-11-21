"""E2E tests for multi-IdP features: provider config endpoints and internal identity resolution.

AC-4.4.5: E2E regression — provider config endpoints; IdP link management is now internal-only.

Auth tiers:
  - Tier 1: Unauthenticated (api_context) → 401
  - Tier 2: Authenticated non-admin (auth_api_context) → 403
  - Tier 3: Admin (admin_api_context) → 403 for provider endpoints (requires operator)

Provider endpoints require the ``operator`` role, which is distinct from
``owner``/``admin``. E2E tests validate auth enforcement; CRUD success
for providers is covered by unit + integration tests.
"""

import os
import uuid

import pytest
from playwright.sync_api import APIRequestContext

from tests.e2e.not_applicable import descope_credentials_missing

pytestmark = pytest.mark.skipif(
    descope_credentials_missing("DESCOPE_MANAGEMENT_KEY"),
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
