"""Integration tests for session lifecycle against a live Descope instance."""

import pytest


@pytest.mark.anyio
async def test_logout_with_valid_token(client, access_token):
    """Logout should succeed with a valid Descope token."""
    response = await client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "logged_out"
    assert data["sub"] is not None


@pytest.mark.anyio
async def test_logout_rejects_expired_token(client, expired_token):
    """Logout should reject an expired token."""
    response = await client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_logout_rejects_missing_auth(client):
    """Logout should reject requests without authentication."""
    response = await client.post("/api/auth/logout")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_session_token_validates_then_expires(client, access_token, expired_token):
    """A valid token should work, and an expired one should be rejected.

    This verifies the session lifecycle: tokens are accepted while valid
    and rejected after expiration.
    """
    # Valid token works
    valid_response = await client.get(
        "/api/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert valid_response.status_code == 200

    # Expired token is rejected
    expired_response = await client.get(
        "/api/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert expired_response.status_code == 401
