"""Unit tests for app.outbound — the HTTP client helper (docs/conventions.md §4–§6)."""

import inspect

import httpx
import pytest

from app.outbound import (
    OutboundClient,
    OutboundResponse,
    UpstreamStatusError,
    UpstreamTransportError,
    outbound_client,
)


class TestOutboundClient:
    @pytest.mark.anyio
    async def test_builds_async_client_with_the_given_timeout(self):
        async with outbound_client(timeout=7.5) as client:
            assert isinstance(client, httpx.AsyncClient)
            assert client.timeout == httpx.Timeout(7.5)

    def test_timeout_is_required_and_keyword_only(self):
        """§6: no call site can fall back to an implicit timeout."""
        param = inspect.signature(outbound_client).parameters["timeout"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty
        with pytest.raises(TypeError):
            outbound_client()  # type: ignore[call-arg]

    @pytest.mark.anyio
    async def test_descope_logout_passes_an_explicit_five_second_timeout(self, monkeypatch):
        """logout.py passes 5.0, the timeout it used to get implicitly from httpx (§6)."""
        from unittest.mock import AsyncMock, MagicMock

        from app.middleware.providers import descope_config
        from app.services import logout

        monkeypatch.setenv("DESCOPE_PROJECT_ID", "proj")
        monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "key")
        client = AsyncMock()
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = client
        monkeypatch.setattr(logout, "outbound_client", factory)

        await logout.LOGOUT_STRATEGIES["management"](descope_config("proj"), {"sub": "U1"}, None)

        factory.assert_called_once_with(timeout=5.0)
        client.post.assert_awaited_once()


class TestAliases:
    def test_aliases_are_the_httpx_types(self):
        """Aliases, not wrappers: `except UpstreamStatusError` catches what httpx raises."""
        assert OutboundClient is httpx.AsyncClient
        assert OutboundResponse is httpx.Response
        assert UpstreamStatusError is httpx.HTTPStatusError
        assert UpstreamTransportError is httpx.RequestError

    def test_status_error_is_caught_by_alias(self):
        request = httpx.Request("GET", "https://example.invalid/")
        response = httpx.Response(503, request=request)
        with pytest.raises(UpstreamStatusError) as exc_info:
            response.raise_for_status()
        assert exc_info.value.response.status_code == 503

    def test_transport_error_is_caught_by_alias(self):
        with pytest.raises(UpstreamTransportError):
            raise httpx.ConnectError("refused")
