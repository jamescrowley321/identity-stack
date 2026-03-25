"""Unit tests for rate limiting middleware."""

import pytest
from fastapi import APIRouter, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from app.middleware.rate_limit import get_rate_limit_key


def _create_test_app(default_limit: str = "3/minute", auth_limit: str = "2/minute"):
    """Create a minimal FastAPI app with rate limiting for testing."""
    limiter = Limiter(key_func=get_remote_address, default_limits=[default_limit])

    app = FastAPI()
    app.state.limiter = limiter

    def _handler(request: Request, exc: RateLimitExceeded):
        from starlette.responses import JSONResponse

        response = JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
        response.headers["Retry-After"] = "60"
        return response

    app.add_exception_handler(RateLimitExceeded, _handler)
    app.add_middleware(SlowAPIMiddleware)

    router = APIRouter()

    @router.get("/health")
    @limiter.exempt
    async def health(request: Request):
        return {"status": "ok"}

    @router.get("/data")
    async def data(request: Request):
        return {"items": []}

    @router.post("/auth/action")
    @limiter.limit(auth_limit)
    async def auth_action(request: Request):
        return {"status": "ok"}

    app.include_router(router)
    return app


@pytest.fixture
def rate_limited_app():
    return _create_test_app(default_limit="3/minute", auth_limit="2/minute")


@pytest.fixture
async def rate_client(rate_limited_app):
    transport = ASGITransport(app=rate_limited_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_requests_within_limit_succeed(rate_client):
    """Requests within the rate limit should succeed."""
    for _ in range(3):
        response = await rate_client.get("/data")
        assert response.status_code == 200


async def test_exceeding_limit_returns_429(rate_client):
    """Exceeding the rate limit should return 429."""
    for _ in range(3):
        await rate_client.get("/data")
    response = await rate_client.get("/data")
    assert response.status_code == 429


async def test_429_response_has_retry_after(rate_client):
    """429 response should include Retry-After header."""
    for _ in range(3):
        await rate_client.get("/data")
    response = await rate_client.get("/data")
    assert response.status_code == 429
    assert "Retry-After" in response.headers


async def test_429_response_body_is_json(rate_client):
    """429 response body should be JSON with detail message."""
    for _ in range(3):
        await rate_client.get("/data")
    response = await rate_client.get("/data")
    assert response.status_code == 429
    body = response.json()
    assert body["detail"] == "Rate limit exceeded"


async def test_health_endpoint_exempt(rate_client):
    """Health endpoint should not be rate limited."""
    for _ in range(10):
        response = await rate_client.get("/health")
        assert response.status_code == 200


async def test_auth_endpoint_has_stricter_limit(rate_client):
    """Auth-sensitive endpoints should have a stricter rate limit."""
    # Auth limit is 2/minute, so 3rd request should fail
    for _ in range(2):
        response = await rate_client.post("/auth/action")
        assert response.status_code == 200
    response = await rate_client.post("/auth/action")
    assert response.status_code == 429


async def test_default_limit_is_per_route():
    """Default limits apply per-route per-key, so hitting one route doesn't exhaust another."""
    app = _create_test_app(default_limit="2/minute")

    router = APIRouter()

    @router.get("/a")
    async def a(request: Request):
        return {"a": True}

    @router.get("/b")
    async def b(request: Request):
        return {"b": True}

    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Hit /a twice (at limit)
        for _ in range(2):
            response = await client.get("/a")
            assert response.status_code == 200
        # /a is now exhausted
        response = await client.get("/a")
        assert response.status_code == 429
        # /b should still have its own quota
        response = await client.get("/b")
        assert response.status_code == 200


class TestGetRateLimitKey:
    """Test the custom rate limit key function."""

    def test_returns_ip_when_no_claims(self):
        """Should return IP when no claims are set."""

        class FakeState:
            pass

        class FakeClient:
            host = "192.168.1.1"

        class FakeRequest:
            state = FakeState()
            client = FakeClient()

        result = get_rate_limit_key(FakeRequest())
        assert result == "192.168.1.1"

    def test_returns_sub_when_claims_present(self):
        """Should return user sub when claims are available."""

        class FakeState:
            claims = {"sub": "user-123", "iss": "descope"}

        class FakeClient:
            host = "192.168.1.1"

        class FakeRequest:
            state = FakeState()
            client = FakeClient()

        result = get_rate_limit_key(FakeRequest())
        assert result == "user-123"

    def test_falls_back_to_ip_when_no_sub(self):
        """Should fall back to IP when claims exist but sub is missing."""

        class FakeState:
            claims = {"iss": "descope"}

        class FakeClient:
            host = "10.0.0.1"

        class FakeRequest:
            state = FakeState()
            client = FakeClient()

        result = get_rate_limit_key(FakeRequest())
        assert result == "10.0.0.1"

    def test_handles_claims_none_gracefully(self):
        """Should handle None claims without error."""

        class FakeState:
            claims = None

        class FakeClient:
            host = "10.0.0.1"

        class FakeRequest:
            state = FakeState()
            client = FakeClient()

        result = get_rate_limit_key(FakeRequest())
        assert result == "10.0.0.1"


async def test_main_app_health_not_rate_limited():
    """The main app's health endpoint should not be rate limited."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(5):
            response = await client.get("/api/health/live")
            assert response.status_code == 200
