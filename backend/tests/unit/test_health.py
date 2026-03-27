"""Unit tests for the health check endpoints."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import health as health_module


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the health check cache and lock between tests."""
    health_module._cache["result"] = None
    health_module._cache["timestamp"] = 0.0
    health_module._cache["ttl"] = health_module.CACHE_TTL_HEALTHY
    health_module._cache_lock = None
    health_module._http_client = None
    yield
    health_module._cache["result"] = None
    health_module._cache["timestamp"] = 0.0
    health_module._cache["ttl"] = health_module.CACHE_TTL_HEALTHY
    health_module._cache_lock = None
    health_module._http_client = None


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --- Liveness ---


@pytest.mark.anyio
async def test_liveness_returns_ok(client):
    response = await client.get("/api/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_liveness_no_auth_required(client):
    """Liveness endpoint should not require authentication."""
    response = await client.get("/api/health/live")
    assert response.status_code == 200


# --- Readiness / Health (both use same logic) ---


@pytest.mark.anyio
@patch("app.routers.health._check_descope")
@patch("app.routers.health._check_database")
async def test_health_healthy(mock_db, mock_descope, client):
    mock_db.return_value = "ok"
    mock_descope.return_value = "ok"

    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["dependencies"]["database"] == "ok"
    assert body["dependencies"]["descope"] == "ok"


@pytest.mark.anyio
@patch("app.routers.health._check_descope")
@patch("app.routers.health._check_database")
async def test_readiness_healthy(mock_db, mock_descope, client):
    mock_db.return_value = "ok"
    mock_descope.return_value = "ok"

    response = await client.get("/api/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["dependencies"]["database"] == "ok"
    assert body["dependencies"]["descope"] == "ok"


@pytest.mark.anyio
@patch("app.routers.health._check_descope")
@patch("app.routers.health._check_database")
async def test_readiness_descope_down(mock_db, mock_descope, client):
    mock_db.return_value = "ok"
    mock_descope.return_value = "error"

    response = await client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["database"] == "ok"
    assert body["dependencies"]["descope"] == "error"


@pytest.mark.anyio
@patch("app.routers.health._check_descope")
@patch("app.routers.health._check_database")
async def test_readiness_database_down(mock_db, mock_descope, client):
    mock_db.return_value = "error"
    mock_descope.return_value = "ok"

    response = await client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["database"] == "error"
    assert body["dependencies"]["descope"] == "ok"


@pytest.mark.anyio
@patch("app.routers.health._check_descope")
@patch("app.routers.health._check_database")
async def test_readiness_both_down(mock_db, mock_descope, client):
    mock_db.return_value = "error"
    mock_descope.return_value = "error"

    response = await client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["database"] == "error"
    assert body["dependencies"]["descope"] == "error"


@pytest.mark.anyio
@patch("app.routers.health._check_descope")
@patch("app.routers.health._check_database")
async def test_health_caching(mock_db, mock_descope, client):
    """Second call within TTL should use cached result, not call checks again."""
    mock_db.return_value = "ok"
    mock_descope.return_value = "ok"

    r1 = await client.get("/api/health")
    assert r1.status_code == 200
    assert mock_db.call_count == 1
    assert mock_descope.call_count == 1

    # Second call should be cached
    r2 = await client.get("/api/health")
    assert r2.status_code == 200
    assert mock_db.call_count == 1  # Not called again
    assert mock_descope.call_count == 1  # Not called again


@pytest.mark.anyio
@patch("app.routers.health._check_descope")
@patch("app.routers.health._check_database")
async def test_health_cache_ttl_expiry(mock_db, mock_descope, client):
    """After TTL expires, checks should run again."""
    mock_db.return_value = "ok"
    mock_descope.return_value = "ok"

    r1 = await client.get("/api/health")
    assert r1.status_code == 200
    assert mock_db.call_count == 1

    # Simulate TTL expiry by backdating the cache timestamp
    health_module._cache["timestamp"] = time.monotonic() - health_module.CACHE_TTL_HEALTHY - 1

    r2 = await client.get("/api/health")
    assert r2.status_code == 200
    assert mock_db.call_count == 2  # Called again after TTL expiry
    assert mock_descope.call_count == 2


@pytest.mark.anyio
@patch("app.routers.health._check_descope")
@patch("app.routers.health._check_database")
async def test_degraded_uses_short_cache_ttl(mock_db, mock_descope, client):
    """Degraded results should use a shorter cache TTL for faster recovery detection."""
    mock_db.return_value = "error"
    mock_descope.return_value = "ok"

    r1 = await client.get("/api/health")
    assert r1.status_code == 503

    # Verify the degraded TTL was set
    assert health_module._cache["ttl"] == health_module.CACHE_TTL_DEGRADED


@pytest.mark.anyio
async def test_health_no_auth_required(client):
    """Health endpoint should not require authentication."""
    response = await client.get("/api/health")
    assert response.status_code != 401


@pytest.mark.anyio
async def test_readiness_no_auth_required(client):
    """Readiness endpoint should not require authentication."""
    response = await client.get("/api/health/ready")
    assert response.status_code != 401


# --- Unit tests for check functions ---


class TestCheckDatabase:
    def test_returns_ok_when_connected(self):
        result = health_module._check_database_sync()
        assert result == "ok"

    @patch("app.routers.health.engine")
    def test_returns_error_on_failure(self, mock_engine):
        mock_engine.connect.side_effect = Exception("connection refused")
        result = health_module._check_database_sync()
        assert result == "error"

    @patch("app.routers.health.engine")
    def test_error_does_not_leak_exception_type(self, mock_engine):
        """Error responses should not contain exception class names."""
        mock_engine.connect.side_effect = ConnectionRefusedError("refused")
        result = health_module._check_database_sync()
        assert result == "error"
        assert "ConnectionRefusedError" not in result


class TestCheckDescope:
    @pytest.mark.anyio
    @patch.dict("os.environ", {"DESCOPE_PROJECT_ID": ""}, clear=False)
    async def test_returns_not_configured_when_no_project_id(self):
        result = await health_module._check_descope()
        assert result == "not_configured"

    @pytest.mark.anyio
    @patch("app.routers.health._get_http_client")
    @patch.dict(
        "os.environ",
        {"DESCOPE_PROJECT_ID": "test-project", "DESCOPE_BASE_URL": "https://api.descope.com"},
        clear=False,
    )
    async def test_returns_ok_on_success(self, mock_get_client):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_client

        result = await health_module._check_descope()
        assert result == "ok"

    @pytest.mark.anyio
    @patch("app.routers.health._get_http_client")
    @patch.dict(
        "os.environ",
        {"DESCOPE_PROJECT_ID": "test-project", "DESCOPE_BASE_URL": "https://api.descope.com"},
        clear=False,
    )
    async def test_returns_error_on_failure(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("timeout"))
        mock_get_client.return_value = mock_client

        result = await health_module._check_descope()
        assert result == "error"

    @pytest.mark.anyio
    @patch("app.routers.health._get_http_client")
    @patch.dict(
        "os.environ",
        {"DESCOPE_PROJECT_ID": "test-project", "DESCOPE_BASE_URL": "https://api.descope.com"},
        clear=False,
    )
    async def test_error_does_not_leak_exception_type(self, mock_get_client):
        """Error responses should not contain exception class names."""
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=ConnectionError("refused"))
        mock_get_client.return_value = mock_client

        result = await health_module._check_descope()
        assert result == "error"
        assert "ConnectionError" not in result


class TestDescopeUrlValidation:
    def test_accepts_valid_descope_url(self):
        result = health_module._validate_descope_base_url("https://api.descope.com")
        assert result == "https://api.descope.com"

    def test_accepts_subdomain(self):
        result = health_module._validate_descope_base_url("https://us1.descope.com")
        assert result == "https://us1.descope.com"

    def test_rejects_non_descope_url(self):
        result = health_module._validate_descope_base_url("https://evil.example.com")
        assert result == health_module._DEFAULT_DESCOPE_BASE_URL

    def test_rejects_http_url(self):
        result = health_module._validate_descope_base_url("http://api.descope.com")
        assert result == health_module._DEFAULT_DESCOPE_BASE_URL

    def test_rejects_url_with_path(self):
        result = health_module._validate_descope_base_url("https://api.descope.com/evil")
        assert result == health_module._DEFAULT_DESCOPE_BASE_URL

    def test_rejects_internal_url(self):
        result = health_module._validate_descope_base_url("http://169.254.169.254")
        assert result == health_module._DEFAULT_DESCOPE_BASE_URL


class TestHealthyWithDescopeNotConfigured:
    """When DESCOPE_PROJECT_ID is empty, descope status is 'not_configured' but overall is healthy."""

    @pytest.mark.anyio
    @patch("app.routers.health._check_database")
    @patch.dict("os.environ", {"DESCOPE_PROJECT_ID": ""}, clear=False)
    async def test_healthy_when_descope_not_configured(self, mock_db, client):
        mock_db.return_value = "ok"

        response = await client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["dependencies"]["descope"] == "not_configured"
