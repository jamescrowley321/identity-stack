"""Unit tests for structured logging and correlation ID middleware."""

import logging
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.logging_config import CorrelationIdFilter, correlation_id_var, get_logger, setup_logging
from app.main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestCorrelationIdFilter:
    def test_adds_correlation_id_to_record(self):
        """Filter should add correlation_id from context var to log records."""
        token = correlation_id_var.set("test-cid-123")
        try:
            record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
            f = CorrelationIdFilter()
            f.filter(record)
            assert record.correlation_id == "test-cid-123"
        finally:
            correlation_id_var.reset(token)

    def test_default_when_no_context(self):
        """Filter should use default '-' when no correlation ID is set."""
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        f = CorrelationIdFilter()
        f.filter(record)
        assert record.correlation_id == "-"


class TestGetLogger:
    def test_returns_logger_with_name(self):
        """get_logger should return a logger with the given name."""
        logger = get_logger("test.module")
        assert logger.name == "test.module"


class TestSetupLogging:
    def test_configures_root_logger(self):
        """setup_logging should configure the root logger with a handler."""
        setup_logging()
        root = logging.getLogger()
        assert len(root.handlers) >= 1
        # Verify our filter is attached
        has_filter = any(isinstance(f, CorrelationIdFilter) for f in root.handlers[0].filters)
        assert has_filter


@pytest.mark.anyio
async def test_correlation_id_in_response_header(client):
    """Health endpoint should return X-Correlation-ID header."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    cid = response.headers.get("X-Correlation-ID")
    assert cid is not None
    # Should be a valid UUID
    uuid.UUID(cid)


@pytest.mark.anyio
async def test_accepts_incoming_correlation_id(client):
    """Middleware should accept and echo back an incoming X-Correlation-ID."""
    custom_cid = "my-trace-id-999"
    response = await client.get("/api/health", headers={"X-Correlation-ID": custom_cid})
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == custom_cid


@pytest.mark.anyio
async def test_each_request_gets_unique_correlation_id(client):
    """Each request without an incoming ID should get a unique correlation ID."""
    r1 = await client.get("/api/health")
    r2 = await client.get("/api/health")
    cid1 = r1.headers["X-Correlation-ID"]
    cid2 = r2.headers["X-Correlation-ID"]
    assert cid1 != cid2


@pytest.mark.anyio
async def test_correlation_id_in_401_response(client):
    """401 responses should include correlation_id in the JSON body."""
    response = await client.get("/api/me")
    assert response.status_code == 401
    body = response.json()
    assert "correlation_id" in body
    # Should match the response header
    assert body["correlation_id"] == response.headers["X-Correlation-ID"]


@pytest.mark.anyio
async def test_auth_failure_logs_warning(client, caplog):
    """Token validation failure should produce a warning log."""
    with caplog.at_level(logging.WARNING, logger="app.middleware.auth"):
        await client.get("/api/me", headers={"Authorization": "Bearer invalid-token"})
    assert any("auth.token_invalid" in r.message for r in caplog.records)


@pytest.mark.anyio
async def test_missing_auth_logs_info(client, caplog):
    """Missing auth header should produce an info log."""
    with caplog.at_level(logging.INFO, logger="app.middleware.auth"):
        await client.get("/api/me")
    assert any("auth.missing_header" in r.message for r in caplog.records)


@pytest.mark.anyio
async def test_correlation_id_on_security_headers_response(client):
    """Correlation ID should be present alongside security headers."""
    response = await client.get("/api/health")
    assert "X-Correlation-ID" in response.headers
    assert "X-Content-Type-Options" in response.headers
