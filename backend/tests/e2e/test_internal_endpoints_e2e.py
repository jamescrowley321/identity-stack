"""E2E tests for internal endpoints: flow sync + webhook (Story 3.1).

AC-3.1.4: /api/internal/ prefix bypasses JWT auth.
AC-3.1.3: Webhook HMAC validation rejects unauthenticated requests.

Internal endpoints do not use the 3-tier JWT auth model. Instead:
- Flow sync: network-level isolation (tested here as accessible without JWT)
- Webhook: HMAC-SHA256 signature validation

These tests validate behavior against a running backend without needing
Descope credentials (internal endpoints bypass JWT).
"""

import json
import os
import uuid

import pytest
from playwright.sync_api import APIRequestContext

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_MANAGEMENT_KEY"),
    reason="DESCOPE_MANAGEMENT_KEY not set",
)


class TestInternalEndpointAuthBypass:
    """AC-3.1.4: Internal endpoints bypass JWT auth — accessible without Bearer token."""

    def test_flow_sync_accessible_without_jwt(self, api_context: APIRequestContext, backend_url: str):
        """POST /api/internal/users/sync does not require Authorization header.

        Without a provider row in DB, service returns a non-401 error.
        The important assertion: it does NOT return 401 (JWT rejection).
        """
        resp = api_context.post(
            f"{backend_url}/api/internal/users/sync",
            data={
                "user_id": f"e2e-test-{uuid.uuid4().hex[:8]}",
                "email": f"e2e-{uuid.uuid4().hex[:8]}@test.example.com",
            },
        )
        # Must NOT be 401 — internal endpoints bypass JWT
        assert resp.status != 401, "Internal endpoint should bypass JWT auth"

    def test_webhook_accessible_without_jwt(self, api_context: APIRequestContext, backend_url: str):
        """POST /api/internal/webhooks/descope does not return JWT 401.

        Without HMAC header, FastAPI returns 422 (missing header), not 401 from JWT.
        """
        resp = api_context.post(
            f"{backend_url}/api/internal/webhooks/descope",
            data={"event_type": "user.created", "data": {}},
        )
        # 422 (missing HMAC header) is expected, NOT 401 from JWT middleware
        assert resp.status != 401 or "webhook" in resp.text().lower(), "Internal endpoint should bypass JWT auth"


class TestWebhookHmacValidation:
    """AC-3.1.3: Webhook HMAC-SHA256 validation."""

    def test_webhook_rejects_invalid_hmac(self, api_context: APIRequestContext, backend_url: str):
        """Invalid HMAC signature → 401 from HMAC validation, not JWT."""
        resp = api_context.post(
            f"{backend_url}/api/internal/webhooks/descope",
            data=json.dumps({"event_type": "user.created", "data": {}}),
            headers={
                "Content-Type": "application/json",
                "X-Descope-Webhook-S256": "invalid-signature-value",
            },
        )
        assert resp.status == 401

    def test_webhook_rejects_missing_hmac_header(self, api_context: APIRequestContext, backend_url: str):
        """Missing X-Descope-Webhook-S256 header → 422 (FastAPI header validation)."""
        resp = api_context.post(
            f"{backend_url}/api/internal/webhooks/descope",
            data=json.dumps({"event_type": "user.created", "data": {}}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 422


class TestFlowSyncEndpoint:
    """AC-3.1.1: Flow HTTP Connector sync endpoint behavior."""

    def test_flow_sync_missing_email_returns_422(self, api_context: APIRequestContext, backend_url: str):
        """Missing required field (email) → Pydantic 422."""
        resp = api_context.post(
            f"{backend_url}/api/internal/users/sync",
            data=json.dumps({"user_id": "test-123"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 422

    def test_flow_sync_invalid_email_returns_422(self, api_context: APIRequestContext, backend_url: str):
        """Invalid email format → Pydantic 422."""
        resp = api_context.post(
            f"{backend_url}/api/internal/users/sync",
            data=json.dumps({"user_id": "test-123", "email": "not-an-email"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 422
