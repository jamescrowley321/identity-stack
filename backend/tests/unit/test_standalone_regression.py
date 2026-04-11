"""Standalone regression tests — proves gateway headers don't bypass auth.

In standalone mode (default), the presence of Tyk gateway headers like
``X-Tyk-Request-ID`` must NOT cause the backend to skip authentication.
``TokenValidationMiddleware`` must still enforce JWT validation regardless of
any forwarded headers — closing the "an attacker forges a Tyk header on a
direct request to the backend" hole.

These tests are the standalone-mode counterpart to
``test_gateway_claims_middleware.py``'s defense-in-depth tests, both of
which were motivated by issue #240 (Tyk silently not in front of the
backend for four days while ``tyk/entrypoint.sh`` was broken).
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_forged_tyk_header_without_auth_returns_401(client):
    """X-Tyk-Request-ID header alone must NOT bypass authentication."""
    response = await client.get(
        "/api/me",
        headers={"X-Tyk-Request-ID": "forged-request-id-12345"},
    )
    assert response.status_code == 401
    detail = response.json().get("detail", "")
    assert "Missing" in detail


@pytest.mark.anyio
async def test_forged_tyk_header_with_invalid_token_returns_401(client):
    """X-Tyk-Request-ID + invalid Bearer credential must still be rejected."""
    response = await client.get(
        "/api/me",
        headers={
            "X-Tyk-Request-ID": "forged-request-id-12345",
            "Authorization": "Bearer invalid.token.here",
        },
    )
    assert response.status_code == 401
    detail = response.json().get("detail", "")
    assert "Invalid" in detail


@pytest.mark.anyio
async def test_forged_gateway_headers_without_auth_returns_401(client):
    """Multiple forged gateway headers must NOT bypass authentication."""
    response = await client.get(
        "/api/me",
        headers={
            "X-Tyk-Request-ID": "forged-request-id-12345",
            "X-Forwarded-For": "10.0.0.1",
            "X-Forwarded-Proto": "https",
            "X-Real-IP": "10.0.0.1",
        },
    )
    assert response.status_code == 401
    detail = response.json().get("detail", "")
    assert "Missing" in detail


@pytest.mark.anyio
async def test_health_endpoint_unaffected_by_gateway_headers(client):
    """Health endpoint remains accessible with or without gateway headers."""
    response = await client.get(
        "/api/health",
        headers={"X-Tyk-Request-ID": "forged-request-id-12345"},
    )
    assert response.status_code == 200
