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
  A user with several must send the header, and the SPA has to send it.
- `get_tenant_id` now depends on the canonical memberships. For a Descope principal that
  dependency returns without touching the database. For a canonical provider the
  memberships are read once per request and shared by the dependencies on that route.
- A client can ask for any tenant id. The answer for a tenant the caller is not in is the
  same 403 as for a tenant that does not exist, so the header is not an existence oracle.
- `require_fga` and `get_tenant_claims` still read Descope claims. The routes behind them
  serve Descope principals only until the contexts that own them move.
- Decisions for a well-formed Descope token are unchanged. A malformed one now fails
  closed everywhere: a `dct` that is not a string names no tenant, and a `tenants` claim,
  a tenant entry or a role list of the wrong type grants nothing. Some of those used to
  surface as an unhandled error from `require_role`.

## Amends

[ADR-0003](0003-authorization-as-route-dependencies.md): the tenant no longer comes from
the `dct` claim alone. Authorization is still declared as route dependencies.
