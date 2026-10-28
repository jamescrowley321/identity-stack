"""The one place the backend constructs an HTTP client.

Outbound HTTP from ``app/`` goes through this module (docs/conventions.md §6–§8):
build clients with ``outbound_client`` and catch upstream failures with
``UpstreamStatusError`` and ``UpstreamTransportError``. No other module in
``app/`` imports ``httpx``; Ruff's ``banned-api`` rule (TID251) enforces it.

The aliases below are the httpx types themselves, not wrappers, so an
``except UpstreamStatusError`` catches exactly what ``except
httpx.HTTPStatusError`` caught and ``exc.response`` is still an httpx response.

Clients are owned by whoever built them. The process-wide client is built once
in ``main.lifespan`` and closed there; a client built per call MUST be closed by
that call (``async with outbound_client(...)``).
"""

from __future__ import annotations

import httpx

OutboundClient = httpx.AsyncClient
OutboundResponse = httpx.Response

# A non-2xx response surfaced by ``response.raise_for_status()``.
UpstreamStatusError = httpx.HTTPStatusError
# The request never produced a response: DNS, connect, TLS, read timeout, etc.
UpstreamTransportError = httpx.RequestError


def outbound_client(*, timeout: float) -> OutboundClient:
    """Build an async HTTP client with an explicit timeout, in seconds.

    ``timeout`` is keyword-only and has no default on purpose: every outbound
    call carries a timeout chosen by its caller, and a call site cannot get
    httpx's implicit default by leaving it out. Use 30.0 for Descope management
    calls (the lifespan client) unless the call has a reason to differ.
    """
    return httpx.AsyncClient(timeout=timeout)
