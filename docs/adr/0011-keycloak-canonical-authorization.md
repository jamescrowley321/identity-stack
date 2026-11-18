# ADR-0011: Keycloak principals are authorized from the canonical model

- **Status:** Accepted
- **Date:** 2026-11-18
- **Rules:** [conventions §17](../conventions.md#17-authorization-fails-closed), [§18](../conventions.md#18-one-resolver-decides-the-tenant-and-the-grants) and [§28](../conventions.md#28-a-provider-is-one-registration)
- **Supersedes:** [ADR-0010](0010-keycloak-token-model.md)

## Context

[ADR-0010](0010-keycloak-token-model.md) read a Keycloak principal's tenant and permissions
from its token: the organization alias was the tenant, the client roles of a per-type "type
client" were the permissions. Client roles are realm-wide, so a token cannot carry a
permission that holds in one organization only. The model needed workarounds no other
provider has: portable and organization-bound types, an Admin API lookup on every
organization-bound request with `type:<type client>` grant markers, a sole-membership
confirmation in the tenant resolver, and a tenant admin of two organizations who was a
tenant admin in neither.

Ory and Zitadel already work the other way: the token says who the caller is, the canonical
store says what the caller may do. The realm was reshaped to match (identity-stack#655): one
public SPA client, an API client whose id a mapper puts in the SPA access token's `aud`, no
role mapped to any user or group, and organization groups as the record of role
assignments. The recorded tokens (`keycloak/fixtures/`) carry no `resource_access` or
`realm_access`.

## Decision

- **Authorization is canonical.** Keycloak has no claims extractor. A deployment lists
  `keycloak` in `CANONICAL_RBAC_PROVIDERS`, as it lists `zitadel`; the resolver then takes the
  tenant from the caller's canonical memberships, selected by `X-Tenant-ID`, and the roles
  and permissions from Postgres. Unlisted, a Keycloak principal is granted nothing.
- **The token is bound at authentication** (401 otherwise, on every route): an RS256
  signature from the realm's keys and `iss` equal to the configured issuer; `aud` contains
  `KEYCLOAK_AUDIENCE`, the API client; `azp` equals `KEYCLOAK_CLIENT_ID`, the SPA; `typ` is
  `Bearer`. The last check is what refuses the SPA's ID token and refresh tokens: Keycloak
  access tokens carry `sid` and `auth_time` too, so Zitadel's heuristic does not apply.
- **The `organization` claim is read once, at first sign-in.** `keycloak_organization_alias`
  is its one reader and the provider's `first_login_organization`. Exactly one alias, mapped
  through `tenants.external_org_id` of the `keycloak` provider, provisions a canonical user,
  its `idp_links` row and the `member` role in that tenant, with the profile from the access
  token (`email`, `email_verified`, the names). No claim, two aliases, an alias no tenant
  names, or a missing or unverified email provisions nothing. The rules of an
  organization-placed first login (§28: the tenant's `domains`, no linking by email) apply
  as they do to Zitadel's.
- **Removed:** the token-model module, the Admin API directory and its client, the
  extractor, the membership list and lookup, the sole-membership confirmation, the
  `token_tenant` field of `GET /api/identity`, `token_authorized_only`, and the variables
  `KEYCLOAK_AUDIENCES`, `KEYCLOAK_APP_CLIENTS`, `KEYCLOAK_PORTABLE_TYPES`,
  `KEYCLOAK_ORGANIZATION_BOUND_TYPES`, `KEYCLOAK_PORTABLE_PERMISSIONS`,
  `KEYCLOAK_DIRECTORY_CLIENT_ID`, `KEYCLOAK_DIRECTORY_CLIENT_SECRET` and
  `MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS`.
- **Startup refuses a half-configured provider:** no `KEYCLOAK_AUDIENCE`, no
  `KEYCLOAK_CLIENT_ID`, or `KEYCLOAK_REQUIRE_HTTPS` off for an issuer whose host is not
  loopback or a `.localhost` name.

## Consequences

- A user in two tenants holds, in each, exactly the roles Postgres gives them there, and
  switches tenant with `X-Tenant-ID`, without a new authorize request.
- No request reads the Keycloak directory: authorization costs one canonical read, as for
  Ory and Zitadel.
- Keycloak and Postgres must agree on users and memberships. A user disabled or removed from
  an organization in the Keycloak console keeps their canonical grants until the pull or
  the reconciliation pass of the two-way sync carries the change in (identity-stack#660,
  identity-stack#661). Deactivating the canonical user refuses them at once (403).
- A deployment that enables Keycloak must seed the tenants' organization links
  (`make keycloak-seed`) before a first sign-in can succeed.
