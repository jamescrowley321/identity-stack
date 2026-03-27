"""E2E tests for authenticated API endpoints.

These tests require DESCOPE_CLIENT_ID and DESCOPE_CLIENT_SECRET env vars.
They use client credentials (access key) flow for OIDC-compatible tokens.
"""

import os

import pytest
from playwright.sync_api import APIRequestContext

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESCOPE_CLIENT_ID") or not os.environ.get("DESCOPE_CLIENT_SECRET"),
    reason="DESCOPE_CLIENT_ID/DESCOPE_CLIENT_SECRET not set",
)


def test_claims_endpoint_returns_token_claims(auth_api_context: APIRequestContext, backend_url: str):
    """GET /api/claims returns decoded access token claims."""
    response = auth_api_context.get(f"{backend_url}/api/claims")
    assert response.status == 200
    body = response.json()
    assert "sub" in body


def test_me_endpoint_returns_identity(auth_api_context: APIRequestContext, backend_url: str):
    """GET /api/me returns ClaimsPrincipal identity."""
    response = auth_api_context.get(f"{backend_url}/api/me")
    assert response.status == 200
    body = response.json()
    assert "identity" in body


def test_tenants_endpoint_lists_user_tenants(auth_api_context: APIRequestContext, backend_url: str):
    """GET /api/tenants returns tenants from JWT."""
    response = auth_api_context.get(f"{backend_url}/api/tenants")
    assert response.status == 200
    body = response.json()
    assert "tenants" in body


def test_validate_id_token(auth_api_context: APIRequestContext, backend_url: str, access_token: str):
    """POST /api/validate-id-token validates the access token."""
    response = auth_api_context.post(
        f"{backend_url}/api/validate-id-token",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status == 200
    body = response.json()
    assert "sub" in body
