# ADR-0009: Providers are registrations, and one resolver decides the tenant and the grants

- **Status:** Accepted
- **Date:** 2026-11-16
- **Rules:** [conventions §18](../conventions.md#18-one-resolver-decides-the-tenant-and-the-grants) and [§28](../conventions.md#28-a-provider-is-one-registration)

## Context

The backend validates tokens from more than one OIDC provider. Two things made each new
provider expensive and easy to get wrong.

Provider settings were named parameters. Ory's issuer, audience and audience switch were
arguments of the provider builder, the middleware factory and both middlewares, and
RP-initiated logout read Ory's client id and redirect URI from the environment inside the
logout service. A second RP-initiated provider meant editing all of them, and would have
logged out with Ory's client id until someone noticed.

The tenant had three readers. `get_tenant_id` read Descope's `dct` claim and nothing else,
so a principal from any other provider got 403 on every route that needs a tenant.
`require_role` and `require_permission` each carried their own copy of the Descope claim
path beside the canonical one, and the canonical path needed exactly one membership,
because a request had no way to say which tenant it was for. A user in two tenants was
refused on every role-gated route.

## Decision

- A provider is one entry in `PROVIDER_REGISTRATIONS`: a function that reads its own
  settings and returns a `ProviderTokenConfig`, or nothing when the deployment does not
  configure it. `configured_providers()` walks the list. The factory, the middlewares and
  the logout router take the list from that one call and name no provider.
- Everything that differs between providers is a field of `ProviderTokenConfig`, including
  the logout client id, the post-logout redirect URI, the HTTPS switch (on by default) and
  the claims extractor.
- The middleware records which provider accepted the token on `request.state.provider`.
- One function, `resolve_tenant_access`, answers "which tenant, which roles, which
  permissions". It reads the provider's claims extractor first. For a provider in
  `CANONICAL_RBAC_PROVIDERS` it reads the canonical memberships instead, and picks the
  tenant the `X-Tenant-ID` header selects among them, or the sole membership. Everything
  else is 403. `get_tenant_id`, `require_role` and `require_permission` all call it.
- The selector is a request header and not a token re-issued per tenant. A header works
  the same for every provider, needs nothing from the provider, and cannot grant anything:
  it is only ever compared with memberships the canonical model already holds for the
  caller.

## Consequences

- A new provider is a registration and, if its tokens carry a tenant, an extractor. No
  other module changes, and a test proves it by registering a provider of its own.
- A provider with no extractor that is not listed in `CANONICAL_RBAC_PROVIDERS` is
  authenticated and then refused on every tenant-scoped route. Descope's claim path is no
  longer what an unlisted provider falls back to, so a `dct` on another provider's token
  grants nothing.
- A canonical-provider user with one membership now gets a tenant from `get_tenant_id`.
  A user with several must send the header. Nothing shipped sends it yet: the SPA's tenant
  switcher does not, so from the SPA such a user is still refused on every tenant-scoped
  route, as before this decision. The switcher sends it with the Zitadel frontend work
  (#568); until then the header serves API clients. Nothing between a client and the
  backend drops it: the Tyk API definition (`tyk/apps/saas-backend.json`) transforms and
  removes no request header, and the backend's CORS policy allows every request header.
- `get_tenant_id` now depends on the canonical memberships. For a Descope principal that
  dependency returns without opening a session or touching the database. For a canonical
  provider it opens a session for the one read, and the memberships are shared by the
  dependencies on that route.
- A canonical-provider principal now has a tenant on routes that were Descope-only just
  because a token with no `dct` got 403 there. Those routes hand the tenant id and the
  caller's `sub` to Descope's Management API and FGA store, where a canonical id names
  nothing: a document created that way would carry an owner relation its creator could
  never use. They keep serving Descope principals only, now by an explicit guard,
  `require_descope_principal`, which reads the provider's `descope_managed` field and
  answers the same 403 as before. A test holds every route that takes a tenant to one of
  two registers, Descope-backed or provider-neutral, so a new route has to choose.
  `POST /api/roles/assign` and `/api/roles/remove` are Descope-backed too: they write
  canonical rows, but `RoleService` mirrors each assignment to Descope with the canonical
  user and tenant ids. The routes over state that belongs to the whole Descope project
  carry the guard as well: the role and permission definitions, which the services mirror
  to Descope, and the FGA schema. `require_role` answers for one tenant, and the selector
  lets a canonical principal with several memberships name the one they administer, which
  would have reached those routes where the sole-membership rule used to refuse them. The
  project-level grant they need is #593. The global routes Descope does not hold (`GET
  /api/users`, `/api/providers`, `/api/sync/status`, `/api/events/recent`) are gated by
  `require_global_role`, for the same reason: on them the header chooses nothing, so a
  canonical principal is served on a sole membership only, as before the header existed.
  No shipped route is provider-neutral yet. The first will be one
  whose service is given a sync adapter chosen by the caller's provider.
- Descope cannot be listed in `CANONICAL_RBAC_PROVIDERS`. A provider that is both
  `descope_managed` and canonical would pass the guard and hand Descope a canonical
  tenant id, so the middleware factory refuses that configuration at startup. Before
  this, listing Descope moved `require_role` to the canonical model and left
  `get_tenant_id` on the `dct` claim.
- A client can ask for any tenant id. The answer for a tenant the caller is not in is the
  same 403 as for a tenant that does not exist, so the header is not an existence oracle.
- `require_fga` and `get_tenant_claims` still read Descope claims. The routes behind them
  serve Descope principals only until the contexts that own them move.
- Building the principal and reading the tenant with the provider's extractor are part of
  authentication in both middlewares. A failure in either is the uniform 401 in standalone
  and in gateway mode. The resolver also fails closed by itself: an extractor that raises,
  that answers with anything but a `TenantClaims`, that names a tenant that is not a
  non-empty string, or whose roles or permissions are not lists of strings, is 403. A
  role list that is a string would otherwise be searched for the required role as a
  substring.
- `build_provider_configs` stays for the tests that build a middleware by hand, as the
  same assembly over explicit values. The middlewares' `ory_*` constructor arguments stay
  with it until those tests move to a provider list (#591).
- Decisions for a well-formed Descope token are unchanged. A malformed one now fails
  closed everywhere: a `dct` that is not a string names no tenant, and a `tenants` claim,
  a tenant entry or a role list of the wrong type grants nothing. Some of those used to
  surface as an unhandled error from `require_role`.

## Amends

[ADR-0003](0003-authorization-as-route-dependencies.md): the tenant no longer comes from
the `dct` claim alone. Authorization is still declared as route dependencies.
