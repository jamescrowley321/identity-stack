"""E2E tests for internal endpoints: flow sync + webhook + reconciliation.

Story 3.1 ACs:
  AC-3.1.4: /api/internal/ prefix bypasses JWT auth.
  AC-3.1.3: Webhook HMAC validation rejects unauthenticated requests.
Story 3.4 ACs:
  AC-3.4.4: E2E regression — authenticated sync endpoint tests.

Internal endpoints do not use the 3-tier JWT auth model. Instead:
- Flow sync: shared secret (X-Flow-Secret header)
- Webhook: HMAC-SHA256 signature validation
- Reconciliation: shared secret (X-Flow-Secret header)

These tests validate behavior against a running backend without needing
Descope credentials (internal endpoints bypass JWT).
"""

import hashlib
import hmac as hmac_mod
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

        Without X-Flow-Secret header, returns 422 (missing header) — NOT 401 from JWT.
        This proves the internal prefix bypasses JWT auth.
        """
        resp = api_context.post(
            f"{backend_url}/api/internal/users/sync",
            data=json.dumps(
                {
                    "user_id": f"e2e-test-{uuid.uuid4().hex[:8]}",
                    "email": f"e2e-{uuid.uuid4().hex[:8]}@test.example.com",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        # 422 (missing X-Flow-Secret header) proves JWT was bypassed
        assert resp.status == 422, f"Expected 422 (missing header), got {resp.status}"

    def test_webhook_accessible_without_jwt(self, api_context: APIRequestContext, backend_url: str):
        """POST /api/internal/webhooks/descope does not return JWT 401.

        Without HMAC header, FastAPI returns 422 (missing header), not 401 from JWT.
        """
        resp = api_context.post(
            f"{backend_url}/api/internal/webhooks/descope",
            data=json.dumps({"event_type": "user.created", "data": {}}),
            headers={"Content-Type": "application/json"},
        )
        # 422 (missing HMAC header) proves JWT was bypassed
        assert resp.status == 422, f"Expected 422 (missing HMAC header), got {resp.status}"


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


# --- AC-3.4.4: E2E regression with valid credentials ---

_has_flow_secret = bool(os.environ.get("DESCOPE_FLOW_SYNC_SECRET"))
_has_webhook_secret = bool(os.environ.get("DESCOPE_WEBHOOK_SECRET"))


@pytest.mark.skipif(not _has_flow_secret, reason="DESCOPE_FLOW_SYNC_SECRET not set")
class TestFlowSyncWithSecret:
    """AC-3.4.4: Flow sync endpoint with valid shared secret."""

    def test_valid_secret_passes_auth_and_returns_json(self, api_context: APIRequestContext, backend_url: str):
        """Flow sync with valid X-Flow-Secret passes auth, returns structured JSON.

        Success → {"user": {...}, "created": true/false}
        Service error → RFC 9457 problem detail with type/title/detail.
        """
        secret = os.environ["DESCOPE_FLOW_SYNC_SECRET"]
        resp = api_context.post(
            f"{backend_url}/api/internal/users/sync",
            data=json.dumps(
                {
                    "user_id": f"e2e-sync-{uuid.uuid4().hex[:8]}",
                    "email": f"e2e-sync-{uuid.uuid4().hex[:8]}@test.example.com",
                    "name": "E2E Sync Test",
                }
            ),
            headers={"Content-Type": "application/json", "X-Flow-Secret": secret},
        )
        assert resp.status != 401, "Valid secret should not be rejected"
        body = resp.json()
        assert isinstance(body, dict)
        if resp.status in (200, 201):
            assert "user" in body, "Success response must include 'user'"
            assert "created" in body, "Success response must include 'created'"
        else:
            # RFC 9457 problem detail
            assert "type" in body, "Error response must be RFC 9457 problem detail"
            assert "title" in body
            assert "detail" in body

    def test_idempotent_replay_returns_consistent_shape(self, api_context: APIRequestContext, backend_url: str):
        """Replaying the same flow sync request twice returns consistent response shape.

        Both requests should either succeed (200/201) or fail identically.
        No duplicate user creation or crashes on replay.
        """
        secret = os.environ["DESCOPE_FLOW_SYNC_SECRET"]
        user_id = f"e2e-replay-{uuid.uuid4().hex[:8]}"
        email = f"e2e-replay-{uuid.uuid4().hex[:8]}@test.example.com"
        payload = json.dumps({"user_id": user_id, "email": email, "name": "Replay Test"})
        headers = {"Content-Type": "application/json", "X-Flow-Secret": secret}

        resp1 = api_context.post(f"{backend_url}/api/internal/users/sync", data=payload, headers=headers)
        resp2 = api_context.post(f"{backend_url}/api/internal/users/sync", data=payload, headers=headers)

        # Both pass auth
        assert resp1.status != 401
        assert resp2.status != 401

        # Consistent response shape (same keys in both)
        body1 = resp1.json()
        body2 = resp2.json()
        assert body1.keys() == body2.keys(), "Replay must return same response shape"

        # If both succeed, second call should return created=False (update, not duplicate)
        if resp1.status in (200, 201) and resp2.status in (200, 201):
            assert body2["created"] is False, "Replay should update, not create duplicate"


@pytest.mark.skipif(not _has_webhook_secret, reason="DESCOPE_WEBHOOK_SECRET not set")
class TestWebhookWithValidHmac:
    """AC-3.4.4: Webhook endpoint with valid HMAC signature."""

    def test_valid_hmac_passes_auth_and_returns_json(self, api_context: APIRequestContext, backend_url: str):
        """Webhook with correctly computed HMAC-SHA256 passes auth."""
        secret = os.environ["DESCOPE_WEBHOOK_SECRET"]
        body_str = json.dumps({"event_type": "user.created", "data": {"user_id": "e2e-hmac-test"}})
        signature = hmac_mod.new(secret.encode(), body_str.encode(), hashlib.sha256).hexdigest()

        resp = api_context.post(
            f"{backend_url}/api/internal/webhooks/descope",
            data=body_str,
            headers={
                "Content-Type": "application/json",
                "X-Descope-Webhook-S256": signature,
            },
        )
        assert resp.status != 401, "Valid HMAC should pass auth"
        body = resp.json()
        assert isinstance(body, dict)


@pytest.mark.skipif(not _has_flow_secret, reason="DESCOPE_FLOW_SYNC_SECRET not set")
class TestReconciliationEndpointE2E:
    """AC-3.4.4: Reconciliation trigger endpoint with valid secret."""

    def test_reconciliation_trigger_returns_json(self, api_context: APIRequestContext, backend_url: str):
        """Reconciliation trigger with valid X-Flow-Secret returns structured JSON.

        Success → {"status": "completed", "stats": {...}}
        Service error → RFC 9457 problem detail.
        """
        secret = os.environ["DESCOPE_FLOW_SYNC_SECRET"]
        resp = api_context.post(
            f"{backend_url}/api/internal/reconciliation/run",
            headers={"Content-Type": "application/json", "X-Flow-Secret": secret},
        )
        assert resp.status != 401, "Valid secret should not be rejected"
        body = resp.json()
        assert isinstance(body, dict)
        if resp.status == 200:
            assert "status" in body, "Success response must include 'status'"
            assert "stats" in body, "Success response must include 'stats'"
        else:
            # RFC 9457 problem detail
            assert "type" in body, "Error response must be RFC 9457 problem detail"
