# ADR-0006: One HTTP client helper and one logger helper

- **Status:** Accepted
- **Date:** 2026-10-28
- **Rules:** [conventions §4–§9](../conventions.md#outbound-http)

## Context

At `fc01974`, eight modules in `backend/app` imported `httpx`, four of them routers
(`users.py`, `documents.py`, `attributes.py`, `fga.py`), and 28 modules imported `logging`. The
routers used `httpx` only to name the exceptions `DescopeManagementClient` lets through.
That still let any router open its own client, and one call site, the Descope logout,
relied on httpx's implicit 5-second timeout without saying so.

## Decision

- `backend/app/outbound.py` is the only module in `app/` that imports `httpx`.
  `outbound_client(*, timeout)` builds every client, and its timeout is required and
  keyword-only. `OutboundClient`, `OutboundResponse`, `UpstreamStatusError` and
  `UpstreamTransportError` are aliases of the httpx types, not wrappers. Existing `except`
  clauses therefore catch exactly what they caught before.
- The logout call passes `timeout=5.0`, the value it was already getting implicitly.
- `backend/app/logbook.py::logbook(name)` returns the module's standard-library logger. It
  is the only caller of `logging.getLogger`. `logging_config.setup_logging` stays the only
  place handlers, formatters and levels are configured. The unused
  `logging_config.get_logger` is removed.
- Ruff `banned-api` (TID251) bans `httpx` and `logging.getLogger` everywhere in `app/`
  except their helper, and G004/G010 keep log calls lazy.

## Consequences

- A reviewer can find every outbound call site by searching for `outbound_client`, and
  every one of them states its timeout.
- Tests that patched `<module>.httpx.AsyncClient` now patch `app.outbound.httpx.AsyncClient`.
  It is the same object, so the assertions are unchanged.
- `logbook` returns a plain `logging.Logger`, so `caplog`, `patch("...logger")` and the
  OpenTelemetry logging instrumentor work as before. "Structured" stays the formatter's job
  in production (JSON with trace ids), not the helper's.
