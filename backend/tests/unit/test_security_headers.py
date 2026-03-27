"""Unit tests for security headers middleware."""

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
async def test_security_headers_present(client):
    """All security headers should be present on responses."""
    response = await client.get("/api/health/live")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["X-XSS-Protection"] == "0"
    assert "Content-Security-Policy" in response.headers


@pytest.mark.anyio
async def test_no_hsts_in_development(client):
    """HSTS should not be set in development mode (default)."""
    response = await client.get("/api/health/live")
    assert "Strict-Transport-Security" not in response.headers


@pytest.mark.anyio
async def test_csp_includes_localhost_in_development(client):
    """CSP in development should allow localhost."""
    response = await client.get("/api/health/live")
    csp = response.headers["Content-Security-Policy"]
    assert "localhost" in csp


@pytest.mark.anyio
async def test_headers_on_authenticated_endpoint(client):
    """Security headers should be present even on 401 responses."""
    response = await client.get("/api/me")
    assert response.status_code == 401
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
