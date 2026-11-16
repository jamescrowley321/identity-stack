# ADR-0010: A Keycloak token is one organization and one user type, and privilege does not cross organizations

- **Status:** Accepted
- **Date:** 2026-11-16
- **Rules:** [conventions §17](../conventions.md#17-authorization-fails-closed), [§18](../conventions.md#18-one-resolver-decides-the-tenant-and-the-grants) and [§28](../conventions.md#28-a-provider-is-one-registration)

## Context

Keycloak is the third provider and the first that carries one tenant and one set of
permissions in the token, in its own claim shapes. The realm model is fixed: an Organization
is the tenant, a confidential "type client" (`member`, `tenant-admin`) is a user type and
the token's `aud`, a client role of the type client named `<domain>:<action>` is a
permission (`resource_access[aud].roles`), and each type has one public app client, the
token's `azp`.

Three properties of Keycloak 26 decide how a token may be read. They were measured on the
local realm and are recorded in `keycloak/fixtures/README.md`.

- Keycloak does not refuse a request for an organization the user is not in. It issues a
  token with no `organization` claim. A request may also name several organizations, and
  the claim then lists them all.
- The middleware's audience check is a membership test. A token with `member` among several
  audiences, or one issued to another client of the realm, passes it.
- Client roles are assigned to the user, not to the user in an organization. A user who
  holds `tenant-admin` roles and belongs to two organizations gets a `tenant-admin` token
  for either, whoever granted the roles and for whichever organization.

## Decision

- **The token model is one pure module**, `app/middleware/keycloak.py`. It imports nothing
  from the application, and every branch without one unambiguous answer refuses. Its
  branches are the ones the library's FastAPI package specifies for single-audience mode
  and its Keycloak claim helpers, so that moving to the library later is a swap.
- **The token is bound at authentication.** A provider may declare a `token_binding`
  (§28), which both middlewares check after the audience check. Keycloak's requires exactly
  one `aud`, a configured type client, and the `azp` configured for it. A token that fails
  gets the uniform 401 on every route, including the ones with no tenant dependency.
- **The tenant and the permissions come from the token alone**, through the provider's
  claims extractor (§18). `organization` must name exactly one alias, as a one-element list
  or a one-key map. Anything else names no tenant, which is 403 and is the membership
  control. A token whose `resource_access[aud].roles` is missing or is not a list of
  strings names no tenant either. An empty list is readable and holds nothing. The tenant is the alias, an opaque string. A Keycloak token has no role, so
  `require_role` refuses every Keycloak principal. `realm_access`, `dct` and `tenants` are
  never read.
- **Every type is declared portable or organization-bound.** A portable type grants only
  the roles on its portable list, and that list may hold only `<domain>:read` and
  `<domain>:list` permissions. A token of an organization-bound type is believed only for a
  caller whose one organization is the token's. The extractor marks its claims
  `sole_membership_required`, and the resolver refuses them unless the provider's
  `sole_membership_lookup` confirmed that tenant. No lookup is wired yet, so every
  organization-bound token is refused. A lookup that fails is 502 and no grant.
- **One answer per failure, in this order:** authentication (401), then what the token
  alone decides (403), then the lookup (403 for "no", 502 for a failure). A token that
  lacks the permission a route needs is refused without a lookup, however the route
  declares its dependencies.
- **Startup refuses a half-configured provider.** With `KEYCLOAK_ISSUER_URL` set, the
  backend starts only with an app client of its own for every type client, every type declared one way
  and not both, a read-only portable list for every portable type, HTTPS enforcement on
  unless the issuer host is loopback or a `.localhost` name, and Keycloak absent from
  `CANONICAL_RBAC_PROVIDERS`. The `.localhost` exemption reads the name and does not
  resolve it, so startup logs a warning whenever enforcement is off.

## Consequences

- A tenant admin who belongs to two organizations is a tenant admin in neither. Per
  organization grants would need Keycloak organization groups or the canonical model, which
  this model does not use.
- A portable permission holds in every organization its user belongs to. Nothing here ends
  access when a user leaves an organization before the token expires (300 seconds by realm
  default). That window is accepted for read-only permissions.
- A write permission on a portable type's token grants nothing. `documents:write` on a
  `member` token is not held.
- The routes that send their tenant to Descope, or gate by role, refuse a Keycloak
  principal. Only the two `/api/access-checks` routes answer one today.
- Adding a user type is configuration: its type client, its app client, its declaration
  and, for a portable type, its list.
