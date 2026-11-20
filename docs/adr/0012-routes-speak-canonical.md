# ADR-0012: Routes speak canonical; providers differ by capability and relationship store

- **Status:** Accepted. The three relationship-store defaults under **Decision** are pending the owner's answer.
- **Date:** 2026-11-19
- **Rules:** [conventions §15](../conventions.md#15-every-tenant-scoped-route-declares-its-authorization), [§17](../conventions.md#17-authorization-fails-closed), [§18](../conventions.md#18-one-resolver-decides-the-tenant-and-the-grants) and [§28](../conventions.md#28-a-provider-is-one-registration)

## Context

The canonical model (Postgres) worked for every provider: Ory, Zitadel and Keycloak principals
were authorized from it ([ADR-0011](0011-keycloak-canonical-authorization.md)), and a sync adapter
per provider pushed its changes. The routes the SPA calls did not. They called Descope's
Management API or FGA store, or read Descope's `dct` and `tenants` claims, and §18 fenced them
with `require_descope_principal`. The guard kept a canonical tenant id or another provider's `sub`
from reaching Descope, but it mapped nothing: signed in as Keycloak's `bob`, an admin of Acme, the
SPA showed Members, Roles, Access Keys and FGA, and each page answered 403
(identity-stack#672).

## Decision

**Routes speak canonical.** A route reads and writes canonical services; a provider is called only
from an adapter or a relationship store. A route the SPA calls serves every provider's principal
that the resolver (§18) authorizes. Descope keeps its claims extractor: a Descope principal is
still decided from its signed `dct` and `tenants`, and reads tenants, members and roles through the
same routes. `require_descope_principal` stays for any code that still hands Descope a tenant id
or a `sub`. No route does so today; the Descope access-key builder calls it.

**Routes whose writes go to the sync provider are fenced on it.** The role assignment, role and
permission definition and member routes write through services that push to the configured sync
adapter (`SYNC_PROVIDER`), which takes the request's tenant and user ids as its own provider's.
`require_sync_provider_principal` serves that provider's principals only. In a Descope deployment
that is exactly the principals `require_descope_principal` passes. Who is such a principal is one
decision, `identity.is_sync_provider_principal`: `GET /api/capabilities` reports it as
`sync_provider_principal`, and the SPA shows the Members and Roles pages only when it is true.

**Capabilities are declared.** A sync adapter declares a frozen set of `Capability`
(`machine_credentials`, `custom_attributes`). `capabilities.served` decides what serves the caller:
an adapter's capabilities serve only its own provider's principals, except its
`canonical_capabilities`, which take no provider id and serve every principal authorized from the
canonical model (Keycloak's machine credentials), never a Descope principal: a Descope `dct` that
names a canonical tenant's id grants nothing there. `GET /api/capabilities` reports the result and the relationship store, and the SPA
shows a page only when its capability is there. A route for a capability that does not serve the
caller answers 404 (`CapabilityMissing`), never a provider's 403.

**One relationship store per deployment.** The FGA and document routes use only the
`RelationshipStore` interface. `DescopeRelationshipStore` holds Descope's FGA calls, keyed on
Descope's ids, and serves Descope principals. `OpenFgaRelationshipStore` is keyed on canonical
tenant and user ids, never the token's `sub`, and serves principals of `CANONICAL_RBAC_PROVIDERS`.
`RELATIONSHIP_STORE` picks one, and defaults to the sync provider's.

Three defaults are pending the owner's answer (epic identity-stack#672, "Owner decisions"):

1. **OpenFGA for every provider that is not Descope, Ory included.** Ory has Keto, but this stack
   runs none. An Ory deployment sets `RELATIONSHIP_STORE=openfga`. The alternative is a
   `KetoRelationshipStore`.
2. **The OpenFGA model lives in Terraform** (`infra/openfga`, the `openfga/openfga` provider), like
   every other provider's configuration. The backend never writes it: `PUT /api/fga/schema`
   answers 409 on OpenFGA. The alternative is a checked-in model file and a make target.
3. **No Descope FGA tuples are migrated.** The stack's FGA data is local test data. The alternative
   is a one-off copy script.

**The authorization rules these routes follow:**

- **Changing the role and permission definitions needs `operator` from a canonical principal**
  (`require_definitions_role`): `operator` held by its sole membership, as on every route over state
  that belongs to no tenant. The definitions belong to no tenant and every tenant's grants read
  them, so a tenant `admin` could otherwise delete the `owner` role in every tenant or grant
  `member` a permission everywhere. A Descope principal keeps `owner` or `admin` from its claims.
  **Listing them** (`GET /api/roles`, `GET /api/permissions`) changes nothing and shows only the
  global definitions, so an `owner`, `admin` or `operator` of the request's tenant lists them
  (`require_role`), as the Roles page needs to assign a role (identity-stack#707).
- **Only an operator touches `operator`, for every provider, Descope included.** Assigning or
  removing `operator` (`/api/roles/assign`, `/api/roles/remove`), inviting with it
  (`/api/members/invite`), and creating, changing, renaming to or from, or deleting its definition
  are refused (403, `RoleGrantRefusal`) unless the caller holds `operator` as `require_global_role`
  decides it. Otherwise a tenant admin could open every tenant's state to itself.
- **Machine credentials are authorized from the credential alone**: its one tenant and its roles
  there, and nothing while it is inactive, expired or deleted. A credential needs at least one
  role, only roles its creator holds, and never `operator` (with one membership,
  `require_global_role` would serve it every tenant). A machine's token is refused
  (`refuse_machine_principal`, 403 `MachinePrincipalRefused`) on every route that changes
  people's roles, memberships, credentials or relations: `/api/keys`, so no credential mints its
  successor; the role assignment and definition routes and `/api/members/*`; the FGA relation and
  schema writes and a document's share and revoke, so no credential makes anyone a document's
  owner; and the IdP-link writes, which decide which account signs in as a person
  (identity-stack#683). Every other write lists why a machine may make it
  (`MACHINE_PERMITTED_WRITES` in `test_descope_backed_routes.py`).
- **A member is acted on only by one who outranks or matches it.** Deactivating, activating or
  removing a member who holds `owner` in the tenant needs the caller to be an owner (403,
  `MemberActionRefusal.OWNER_REQUIRED`); one who holds `operator` needs an operator
  (`operator_grant_check`). No caller deactivates or removes itself (403,
  `MemberActionRefusal.ACTING_ON_SELF`): a deactivated user cannot sign back in to undo it.
- **A role is assigned only to a member of the tenant**, on every adapter (403,
  `access.NotATenantMember`), with the same answer for an unknown user and another tenant's.
  Otherwise a tenant admin could pull another tenant's user, an operator among them, into theirs
  (identity-stack#693).
- **An invitation only creates an account.** The user and its memberships commit together, and
  an email any canonical user already has is refused (409, `MemberRefusal.ACCOUNT_EXISTS`)
  whichever tenant holds it. So an admin cannot enrol another tenant's account or learn that it
  exists.

## Consequences

- A Keycloak admin uses the tenant, member, role, profile, access-key, FGA and document pages in a
  deployment that syncs to Keycloak: it lists the definitions and assigns roles in its tenant, and
  changing a definition needs an operator. The shared e2e visits every page the SPA shows for
  Descope, Keycloak and Zitadel, and fails on any 403.
- A deployment serves the member and role routes to one provider's principals: the sync
  provider's. Any other principal is refused 403 there, and its SPA shows no Members or Roles page.
  CI's Zitadel job is such a deployment: its backend syncs to Descope (identity-stack#709).
- Descope access-key sessions are machine principals under these rules: Descope's `machine_client`
  reader takes a session that names no authentication method (`amr`) for an access key's, so it
  is refused where a machine credential is (identity-stack#699).
- An Ory or Keycloak deployment that adds FGA needs an OpenFGA server and the applied
  `infra/openfga` root, and starts with no relations.
