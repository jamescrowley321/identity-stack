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
  `sole_membership_lookup` confirmed that tenant. Keycloak's lookup reads the caller's
  organizations from the Admin REST API with a read-only service account
  (`KEYCLOAK_DIRECTORY_CLIENT_*`; `realm-management` `view-users` and `view-organizations`,
  the narrowest roles that open the endpoint), always for the token's `sub`. It counts the
  token's organization only where that organization marks the caller as holding the
  token's type: its `type:<type client>` attribute lists the caller's id. A lookup that
  fails, or does not answer within `MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS`, is 502 and no grant.
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
- Sole membership does not show that a role was granted for that organization: Keycloak
  keeps a user's client roles when the user leaves an organization. The grant marker closes
  that: a tenant admin of one organization who joins a second and then leaves the first is
  not marked in the second, so the lookup does not count it and the token is refused. The
  cost is a second place to grant a type: a realm admin maps the client roles and adds the
  user's id to the organization's `type:<type client>` attribute, and removes both when the
  user stops being one. A marker without the roles grants nothing, and roles without the
  marker grant nothing for an organization-bound type.
- A portable permission holds in every organization its user belongs to. Measured on
  Keycloak 26.8.2 (`keycloak/fixtures/README.md`): after a user is removed from an
  organization, the access token they hold for it stays valid until it expires (300 seconds
  by realm default), because the backend verifies it locally and does not ask the directory
  about a portable token; the next refresh is refused (`invalid_grant`, "Invalid
  organization"), and so is `userinfo`. The window is the access token's remaining lifetime,
  and it is accepted for read-only permissions (decision 13).
- A write permission on a portable type's token grants nothing. `documents:write` on a
  `member` token is not held.
- The routes that send their tenant to Descope, or gate by role, refuse a Keycloak
  principal. Only the two `/api/access-checks` routes answer one today.
- Adding a user type is configuration: its type client, its app client, its declaration
  and, for a portable type, its list.
