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

    def test_five_seconds_matches_httpx_implicit_default(self):
        """logout.py passes 5.0 to keep the timeout it had when it relied on httpx's default."""
        assert httpx.AsyncClient().timeout == httpx.Timeout(5.0)


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
