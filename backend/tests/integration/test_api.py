"""Integration tests that validate the API against a live Descope instance."""

import pytest


@pytest.mark.anyio
async def test_health_endpoint(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_me_with_valid_token(client, access_token):
    """Validate that a real Descope token is accepted and claims are extracted."""
    response = await client.get(
        "/api/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200
    data = response.json()
    identity = data["identity"]
    assert identity["is_authenticated"] is True
    claim_types = {c["type"] for c in identity["claims"]}
    assert "sub" in claim_types


@pytest.mark.anyio
async def test_claims_with_valid_token(client, access_token):
    """Validate that raw claims are returned for a valid token."""
    response = await client.get(
        "/api/claims",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "iss" in data
    assert "exp" in data
    assert "sub" in data


@pytest.mark.anyio
async def test_me_rejects_expired_token(client, expired_token):
    """Validate that an expired Descope token is rejected."""
    response = await client.get(
        "/api/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_me_rejects_garbage_token(client):
    response = await client.get(
        "/api/me",
        headers={"Authorization": "Bearer not.a.real.token"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_me_rejects_missing_auth(client):
    response = await client.get("/api/me")
    assert response.status_code == 401
