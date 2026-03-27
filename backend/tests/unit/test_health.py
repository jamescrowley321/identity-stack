"""Unit tests for the health check endpoints."""

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import health as health_module


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the health check cache between tests."""
    health_module._cache["result"] = None
    health_module._cache["timestamp"] = 0.0
    yield
    health_module._cache["result"] = None
    health_module._cache["timestamp"] = 0.0


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
    mock_descope.return_value = "error: connection refused"

    response = await client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["database"] == "ok"
    assert "error" in body["dependencies"]["descope"]


@pytest.mark.anyio
@patch("app.routers.health._check_descope")
@patch("app.routers.health._check_database")
async def test_readiness_database_down(mock_db, mock_descope, client):
    mock_db.return_value = "error: could not connect"
    mock_descope.return_value = "ok"

    response = await client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert "error" in body["dependencies"]["database"]
    assert body["dependencies"]["descope"] == "ok"


@pytest.mark.anyio
@patch("app.routers.health._check_descope")
@patch("app.routers.health._check_database")
async def test_readiness_both_down(mock_db, mock_descope, client):
    mock_db.return_value = "error: db unreachable"
    mock_descope.return_value = "error: timeout"

    response = await client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert "error" in body["dependencies"]["database"]
    assert "error" in body["dependencies"]["descope"]


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
        assert result.startswith("error:")
        assert "Exception" in result


class TestCheckDescope:
    @pytest.mark.anyio
    @patch("app.routers.health.DESCOPE_PROJECT_ID", "")
    async def test_skips_when_no_project_id(self):
        result = await health_module._check_descope()
        assert result == "ok"

    @pytest.mark.anyio
    @patch("app.routers.health.httpx.AsyncClient")
    @patch("app.routers.health.DESCOPE_PROJECT_ID", "test-project")
    async def test_returns_ok_on_success(self, mock_client_cls):
        mock_client = mock_client_cls.return_value.__aenter__.return_value
        mock_resp = mock_client.get.return_value
        mock_resp.raise_for_status.return_value = None

        result = await health_module._check_descope()
        assert result == "ok"

    @pytest.mark.anyio
    @patch("app.routers.health.httpx.AsyncClient")
    @patch("app.routers.health.DESCOPE_PROJECT_ID", "test-project")
    async def test_returns_error_on_failure(self, mock_client_cls):
        mock_client = mock_client_cls.return_value.__aenter__.return_value
        mock_client.get.side_effect = Exception("timeout")

        result = await health_module._check_descope()
        assert result == "error: Exception"
