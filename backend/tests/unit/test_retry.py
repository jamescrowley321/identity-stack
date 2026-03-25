"""Unit tests for Descope API retry logic with exponential backoff."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.descope import DescopeManagementClient, _backoff_delay


@pytest.fixture
def client():
    return DescopeManagementClient("proj-123", "mgmt-key-456", "https://api.descope.com")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _mock_response(status_code=200, json_data=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(f"{status_code}", request=MagicMock(), response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestRetryOnTransientErrors:
    @pytest.mark.anyio
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_retries_on_429(self, mock_cls, mock_sleep, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.side_effect = [
            _mock_response(429),
            _mock_response(200, {"tenants": []}),
        ]

        result = await client.list_tenants()
        assert result == []
        assert mock_http.request.call_count == 2
        mock_sleep.assert_called_once()

    @pytest.mark.anyio
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_retries_on_502(self, mock_cls, mock_sleep, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.side_effect = [
            _mock_response(502),
            _mock_response(200, {"id": "t1"}),
        ]

        result = await client.load_tenant("t1")
        assert result == {"id": "t1"}
        assert mock_http.request.call_count == 2

    @pytest.mark.anyio
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_retries_on_503(self, mock_cls, mock_sleep, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.side_effect = [
            _mock_response(503),
            _mock_response(503),
            _mock_response(200, {"id": "t1"}),
        ]

        result = await client.load_tenant("t1")
        assert result == {"id": "t1"}
        assert mock_http.request.call_count == 3
        assert mock_sleep.call_count == 2

    @pytest.mark.anyio
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_retries_on_connect_error(self, mock_cls, mock_sleep, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.side_effect = [
            httpx.ConnectError("connection refused"),
            _mock_response(200, {"tenants": []}),
        ]

        result = await client.list_tenants()
        assert result == []
        assert mock_http.request.call_count == 2

    @pytest.mark.anyio
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_retries_on_timeout(self, mock_cls, mock_sleep, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.side_effect = [
            httpx.TimeoutException("read timeout"),
            _mock_response(200, {"id": "t1"}),
        ]

        result = await client.load_tenant("t1")
        assert result == {"id": "t1"}
        assert mock_http.request.call_count == 2


class TestNoRetryOnClientErrors:
    @pytest.mark.anyio
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_no_retry_on_400(self, mock_cls, mock_sleep, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.return_value = _mock_response(400)

        with pytest.raises(httpx.HTTPStatusError):
            await client.load_tenant("t1")
        assert mock_http.request.call_count == 1
        mock_sleep.assert_not_called()

    @pytest.mark.anyio
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_no_retry_on_401(self, mock_cls, mock_sleep, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.return_value = _mock_response(401)

        with pytest.raises(httpx.HTTPStatusError):
            await client.load_tenant("t1")
        assert mock_http.request.call_count == 1
        mock_sleep.assert_not_called()

    @pytest.mark.anyio
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_no_retry_on_404(self, mock_cls, mock_sleep, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.return_value = _mock_response(404)

        with pytest.raises(httpx.HTTPStatusError):
            await client.load_tenant("t1")
        assert mock_http.request.call_count == 1
        mock_sleep.assert_not_called()


class TestMaxRetriesExhausted:
    @pytest.mark.anyio
    @patch("app.services.descope.MAX_RETRIES", 2)
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_raises_after_max_retries_status(self, mock_cls, mock_sleep, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.return_value = _mock_response(503)

        with pytest.raises(httpx.HTTPStatusError):
            await client.load_tenant("t1")
        # 1 initial + 2 retries = 3 attempts
        assert mock_http.request.call_count == 3
        assert mock_sleep.call_count == 2

    @pytest.mark.anyio
    @patch("app.services.descope.MAX_RETRIES", 2)
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_raises_after_max_retries_connect_error(self, mock_cls, mock_sleep, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.side_effect = httpx.ConnectError("connection refused")

        with pytest.raises(httpx.ConnectError):
            await client.load_tenant("t1")
        assert mock_http.request.call_count == 3
        assert mock_sleep.call_count == 2


class TestBackoffDelay:
    def test_first_attempt_bounded(self):
        for _ in range(100):
            delay = _backoff_delay(0)
            assert 0 <= delay <= 0.5  # base_delay * 2^0 = 0.5

    def test_second_attempt_bounded(self):
        for _ in range(100):
            delay = _backoff_delay(1)
            assert 0 <= delay <= 1.0  # base_delay * 2^1 = 1.0

    def test_large_attempt_capped_at_max(self):
        for _ in range(100):
            delay = _backoff_delay(20)
            assert 0 <= delay <= 30  # capped at RETRY_MAX_DELAY


class TestRetryLogging:
    @pytest.mark.anyio
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_logs_retry_on_status(self, mock_cls, mock_sleep, client, caplog):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.side_effect = [
            _mock_response(429),
            _mock_response(200, {"tenants": []}),
        ]

        with caplog.at_level(logging.WARNING, logger="app.services.descope"):
            await client.list_tenants()

        assert any("descope.retry" in r.message and "status=429" in r.message for r in caplog.records)

    @pytest.mark.anyio
    @patch("app.services.descope.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_logs_retry_on_connect_error(self, mock_cls, mock_sleep, client, caplog):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.side_effect = [
            httpx.ConnectError("connection refused"),
            _mock_response(200, {"tenants": []}),
        ]

        with caplog.at_level(logging.WARNING, logger="app.services.descope"):
            await client.list_tenants()

        assert any("descope.retry" in r.message and "ConnectError" in r.message for r in caplog.records)
