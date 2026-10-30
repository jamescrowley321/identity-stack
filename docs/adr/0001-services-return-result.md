# ADR-0001: Services return `Result`; routers translate with `result_to_response`

- **Status:** Accepted
- **Date:** 2026-10-28 (recorded retroactively for a decision already in the code)
- **Rules:** [conventions §10–§13](../conventions.md#results-and-errors)

## Context

The canonical identity services (`TenantService`, `RoleService`, `UserService` and the
rest) are called from routers, from the reconciliation job and from inbound sync. When a
service raised `HTTPException`, it tied domain code to one transport. It also meant a
caller outside a request, such as reconciliation, had to catch an HTTP type to learn that
a role did not exist.

## Decision

- A service method returns `Result[T, IdentityError]` from `expression`. A domain outcome
  (not found, conflict, invalid input, provider failure, partial sync) is an
  `Error(<IdentityError subclass>)` value, never an exception.
- `IdentityError` subclasses are frozen dataclasses in `app/errors/identity.py`.
- `app/errors/problem_detail.py::result_to_response` is the single translation point from a
  `Result` to HTTP. `Ok` becomes JSON with the chosen status. `Error` becomes an RFC 9457
  problem detail whose URI, status and title come from `_ERROR_TYPE_MAP`. Detail that may
  leak upstream internals is replaced from `_SANITIZED_DETAIL`.

## Consequences

- Every domain error has one HTTP shape, and a new error kind is not finished until it has
  a mapping. A test enforces that.
- Services stay free of FastAPI imports and can be called from jobs and tests without a
  request.
- The Descope-proxy routers (`accesskeys`, `attributes`, `documents`, `fga`, `users`) call
  `DescopeManagementClient` directly and never hold a `Result`. §12 does not apply to them
  until they move behind a service.
