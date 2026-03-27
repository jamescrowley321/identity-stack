"""Unit tests for security headers middleware."""

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.main import app
from app.middleware.security import SecurityHeadersMiddleware


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_app(environment="development", routes=None):
    """Build a minimal Starlette app with SecurityHeadersMiddleware."""

    async def ok(request: Request):
        return PlainTextResponse("ok")

    test_app = Starlette(routes=routes or [Route("/test", ok)])
    test_app.add_middleware(SecurityHeadersMiddleware, environment=environment)
    return test_app


def _make_raising_app():
    """Build an app whose handler always raises."""

    async def boom(request: Request):
        raise RuntimeError("handler exploded")

    test_app = Starlette(routes=[Route("/boom", boom)])
    test_app.add_middleware(SecurityHeadersMiddleware, environment="production")
    return test_app


# --- Integration tests using the real app (dev mode) ---


@pytest.mark.anyio
async def test_security_headers_present(client):
    """All security headers should be present on responses."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["X-XSS-Protection"] == "0"
    assert "Content-Security-Policy" in response.headers


@pytest.mark.anyio
async def test_no_hsts_in_development(client):
    """HSTS should not be set in development mode (default)."""
    response = await client.get("/api/health")
    assert "Strict-Transport-Security" not in response.headers


@pytest.mark.anyio
async def test_csp_includes_localhost_in_development(client):
    """CSP in development should allow localhost."""
    response = await client.get("/api/health")
    csp = response.headers["Content-Security-Policy"]
    assert "localhost" in csp


@pytest.mark.anyio
async def test_headers_on_authenticated_endpoint(client):
    """Security headers should be present even on 401 responses."""
    response = await client.get("/api/me")
    assert response.status_code == 401
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


# --- M1: Case-insensitive environment detection ---


@pytest.mark.anyio
@pytest.mark.parametrize(
    "env_value",
    ["production", "Production", "PRODUCTION", " production ", " Production "],
)
async def test_production_detected_case_insensitive(env_value):
    """M1: Various casings and whitespace should all resolve to production mode."""
    test_app = _make_app(environment=env_value)
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/test")
    assert response.status_code == 200
    assert "Strict-Transport-Security" in response.headers


@pytest.mark.anyio
@pytest.mark.parametrize("env_value", ["development", "staging", "dev", ""])
async def test_non_production_environments(env_value):
    """Non-production environments should not get HSTS."""
    test_app = _make_app(environment=env_value)
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/test")
    assert "Strict-Transport-Security" not in response.headers


# --- M2: CSP_POLICY env var validation ---


@pytest.mark.anyio
async def test_csp_env_empty_uses_default(monkeypatch):
    """M2: Empty CSP_POLICY should fall back to the default."""
    monkeypatch.setenv("CSP_POLICY", "")
    test_app = _make_app(environment="production")
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/test")
    assert response.headers["Content-Security-Policy"] == "default-src 'self'"


@pytest.mark.anyio
async def test_csp_env_whitespace_uses_default(monkeypatch):
    """M2: Whitespace-only CSP_POLICY should fall back to the default."""
    monkeypatch.setenv("CSP_POLICY", "   ")
    test_app = _make_app(environment="production")
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/test")
    assert response.headers["Content-Security-Policy"] == "default-src 'self'"


@pytest.mark.anyio
@pytest.mark.parametrize("bad_csp", ["*", "default-src *", "script-src unsafe-inline", "script-src unsafe-eval"])
async def test_csp_env_permissive_rejected(monkeypatch, bad_csp):
    """M2: Permissive CSP patterns should be rejected in favor of default."""
    monkeypatch.setenv("CSP_POLICY", bad_csp)
    test_app = _make_app(environment="production")
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/test")
    assert response.headers["Content-Security-Policy"] == "default-src 'self'"


@pytest.mark.anyio
async def test_csp_env_valid_custom_accepted(monkeypatch):
    """M2: A valid custom CSP_POLICY should be used."""
    monkeypatch.setenv("CSP_POLICY", "default-src 'self'; img-src https://cdn.example.com")
    test_app = _make_app(environment="production")
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/test")
    assert response.headers["Content-Security-Policy"] == "default-src 'self'; img-src https://cdn.example.com"


# --- S1: Headers applied on exception ---


@pytest.mark.anyio
async def test_headers_present_on_handler_exception():
    """S1: Security headers should be present even when the handler raises."""
    test_app = _make_raising_app()
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/boom")
    assert response.status_code == 500
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers


# --- S3/S4: New security headers ---


@pytest.mark.anyio
async def test_permissions_policy_header():
    """S3: Permissions-Policy header should restrict sensitive browser APIs."""
    test_app = _make_app()
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/test")
    pp = response.headers["Permissions-Policy"]
    assert "camera=()" in pp
    assert "microphone=()" in pp
    assert "geolocation=()" in pp


@pytest.mark.anyio
async def test_cross_origin_headers():
    """S4: COOP and CORP headers should be present."""
    test_app = _make_app()
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/test")
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"


# --- S5: HSTS preload ---


@pytest.mark.anyio
async def test_hsts_includes_preload_in_production():
    """S5: Production HSTS should include preload directive."""
    test_app = _make_app(environment="production")
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/test")
    hsts = response.headers["Strict-Transport-Security"]
    assert "preload" in hsts
    assert "includeSubDomains" in hsts
    assert "max-age=31536000" in hsts
