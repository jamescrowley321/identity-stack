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


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestCorrelationIdFilter:
    def test_adds_correlation_id_to_record(self):
        token = correlation_id_var.set("test-cid-123")
        try:
            record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
            f = CorrelationIdFilter()
            f.filter(record)
            assert record.correlation_id == "test-cid-123"
        finally:
            correlation_id_var.reset(token)

    def test_default_when_no_context(self):
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        f = CorrelationIdFilter()
        f.filter(record)
        assert record.correlation_id == "-"


class TestGetLogger:
    def test_returns_logger_with_name(self):
        logger = get_logger("test.module")
        assert logger.name == "test.module"


class TestSetupLogging:
    def test_configures_root_logger(self):
        setup_logging()
        root = logging.getLogger()
        assert len(root.handlers) >= 1
        has_filter = any(isinstance(f, CorrelationIdFilter) for f in root.handlers[0].filters)
        assert has_filter


@pytest.mark.anyio
async def test_correlation_id_in_response_header(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    cid = response.headers.get("X-Correlation-ID")
    assert cid is not None
    uuid.UUID(cid)


@pytest.mark.anyio
async def test_accepts_incoming_correlation_id(client):
    custom_cid = "my-trace-id-999"
    response = await client.get("/api/health", headers={"X-Correlation-ID": custom_cid})
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == custom_cid


@pytest.mark.anyio
async def test_rejects_invalid_correlation_id(client):
    """Invalid CIDs should be replaced with a generated UUID."""
    response = await client.get("/api/health", headers={"X-Correlation-ID": "bad<script>id"})
    assert response.status_code == 200
    cid = response.headers["X-Correlation-ID"]
    assert "<script>" not in cid
    uuid.UUID(cid)  # should be a valid UUID
