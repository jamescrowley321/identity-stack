# ADR-0004: Routing-layer guards raise `IdentityErrorRaised`

- **Status:** Accepted
- **Date:** 2026-10-28 (recorded retroactively for a decision already in the code)
- **Rules:** [conventions §14](../conventions.md#14-guards-refuse-by-raising)

## Context

[ADR-0001](0001-services-return-result.md) makes services return errors as values. A
routing-layer guard, such as `routers/idp_links.py::_require_user_in_tenant`, used to
follow the same style and return `Response | None` for its caller to propagate. That is
fail-open by construction. A new route that calls the guard and drops its return value
silently loses tenant isolation, with no type error and no failing test.

## Decision

- A guard refuses by raising. When it refuses with a domain error, it raises
  `app.errors.identity.IdentityErrorRaised(<IdentityError>)`.
- `main.py` registers `identity_error_raised_handler`, which renders the error through
  `result_to_response`. A raised error and a returned one therefore produce byte-identical
  problem details.
- Services are unaffected. They still return `Result` and never raise.

## Consequences

- There is no return value for a caller to forget.
- There are two ways to produce a domain error response, returning a `Result` from a
  handler or raising from a guard, and they share one rendering path.
