# ADR-0003: Authorization is declared as route dependencies

- **Status:** Accepted
- **Date:** 2026-10-28 (recorded retroactively for a decision already in the code)
- **Rules:** [conventions §15–§18](../conventions.md#authorization-and-tenancy)

## Context

Token validation happens in middleware (`TokenValidationMiddleware` in standalone mode,
`GatewayClaimsMiddleware` behind Tyk), which leaves the claims on `request.state`. Every
handler then needs to know the caller's tenant and whether the caller may act. When a
handler checks this in its body, a reviewer has to read the whole handler to know whether
the route is protected. A handler that forgets the check looks the same as one that does
not need it.

## Decision

- The tenant comes from the `get_tenant_id` dependency, which reads Descope's `dct` claim,
  and memberships come from `get_tenant_claims`. Handlers do not read `request.state`
  themselves. The three exceptions are named in conventions §16.
- Authorization is a dependency in the route signature: `require_role(...)`,
  `require_permission(...)` or `require_fga(resource_type, relation, ...)`. Each factory
  returns a closure that FastAPI resolves before the handler runs.
- Descope principals are decided from their signed `dct`/`tenants` claims. Principals from
  a provider in `CANONICAL_RBAC_PROVIDERS` (default `ory`), whose tokens carry no
  authorization claims, are decided from the canonical model, uncached. Every ambiguous
  branch fails closed.

## Consequences

- Whether a route is protected can be read from its signature, and checked mechanically.
  `tests/unit/test_router_authorization.py` walks every route's dependency tree and fails a
  tenant-scoped route that has no authorization dependency.
- Routes that any tenant member may call are a real category, such as reading your own
  roles or listing your own documents. They are listed by name in that test's
  `MEMBER_ROUTES` register, so adding one is a visible, reviewed change.
