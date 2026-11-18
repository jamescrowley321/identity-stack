# Keycloak fixtures

Tokens taken on 2026-11-18 from `quay.io/keycloak/keycloak:26.8.2`, the compose `keycloak-test`
service (`make test-up-keycloak`) with the realm in
[`../realm/identity-stack-realm.json`](../realm/identity-stack-realm.json).

| File | What it is |
|---|---|
| `access-token.json` | Decoded header and claims of the SPA's access token: user `bob` (role assignments `admin` in `acme`, `viewer` in `globex`), client `identity-stack-spa`, scope `openid profile email organization:acme`, authorization code flow with PKCE S256 through the login form. |
| `id-token.json` | The ID token of the same login. |
| `other-client-access-token.json` | `bob`'s access token through `identity-stack-other`, the other public client, with the same scope: a token without the API audience. |
| `discovery.json` | Body of `GET http://keycloak.localhost:8180/realms/identity-stack/.well-known/openid-configuration`, fetched from the host on 2026-11-15 (the compose `keycloak` service). The backend container returned the same document. |

No signature is stored. The signing key is generated per container, so `kid`, `sid`, `jti` and
the times differ on every instance; the issuer is the instance's (`keycloak-test` here). The
claim names and shapes are what the fixtures are for. The users' ids are fixed in the realm
import (table below), so a token's `sub` is the same on every instance.

## The realm

The token says who the caller is; the canonical store (the backend database) says what the caller
may do. Nothing in the realm grants a role that reaches a token.

[`infra/keycloak/`](../../infra/keycloak/README.md) builds the same realm in Terraform. A change
that adds a user, client, role, group, organization or member to the import file adds it there
too; `make keycloak-terraform-check` compares the two.

Every secret and password below is a local fixture. The realm's redirect URIs
(`http://localhost:3000`, post-logout `http://localhost:3000/login`), its SSL mode `none` and the
`admin`/`admin` bootstrap login are for this local container only; do not copy them elsewhere.

| Client | Kind | What |
|---|---|---|
| `identity-stack-spa` | public, standard flow only, PKCE S256, full scope off | The SPA. Default client scopes `basic`, `profile`, `email`, `web-origins`, `acr` (no `roles`); optional `organization`, `offline_access`. An audience mapper (`oidc-audience-mapper`, included client `identity-stack-api`) puts the API client in its access tokens' `aud`, not in its ID tokens'. |
| `identity-stack-api` | confidential, no flows, full scope off | The API: its client id is the audience. Its client roles are the canonical permissions, `<domain>:<action>` (`billing:manage`, `documents:delete`, `documents:read`, `documents:write`, `members:invite`, `members:remove`, `members:update_role`, `projects:create`, `projects:delete`, `projects:read`, `projects:update`, `settings:manage`), and the canonical roles `owner`, `admin`, `member` and `viewer` as composite client roles of their permissions (`owner` all twelve, `admin` all but `billing:manage`, `member` `documents:read`, `documents:write`, `members:invite`, `projects:read`, `projects:update`, `viewer` `documents:read`, `projects:read`). No user and no group holds any of them, and no client is scope-mapped to them. |
| `identity-stack-other` | public, as the SPA, without the audience mapper | Another client, for tests that its tokens are refused. |
| `identity-stack-admin` | confidential, service account only, client scope `kc-admin-api`, full scope off | The backend's Keycloak admin adapter (`KEYCLOAK_ADMIN_CLIENT_ID`, secret `local-only-admin-client`): see "The admin service account" below. |

`admin-permissions` is the client Keycloak creates when fine-grained admin permissions (v2) are
on (`adminPermissionsEnabled`); the import gives it the one permission described below.

### Role assignments are organization groups

Each organization owns four groups, one per canonical role: `owner`, `admin`, `member` and
`viewer`. A user's role in a tenant is membership of that organization's group for the role. The
groups map no role, realm or client, so the membership is the record of the assignment and grants
nothing; the backend never reads it from a token, and no protocol mapper writes groups to one. The
realm has no realm-level groups.

Measured on 26.8.2 (feature `ORGANIZATION` on, which is the default): organization-owned groups
exist. `GET`/`POST /admin/realms/{realm}/organizations/{id}/groups` list and create them, and
`PUT`/`DELETE .../organizations/{id}/groups/{group}/members/{user}` add and remove a member.
Keycloak refuses (400) a user who is not a member of the organization ("User is not member of
the organization"), and refuses the realm group API for these groups (`GET /groups/{id}`,
`PUT /users/{id}/groups/{group}`: "Cannot manage organization related group via non Organization
API"); `GET /users/{id}/groups` does not list them, `GET .../organizations/{id}/members/{user}/groups`
does. A group's role mappings are read at `.../organizations/{id}/groups/{group}/role-mappings`.
Admin events name the resource types `ORGANIZATION_GROUP` and `ORGANIZATION_GROUP_MEMBERSHIP`.
The realm import accepts them: `organizations[].groups` (each with a fixed `id`) and
`organizations[].members[].groups` (group ids). The Terraform provider (`keycloak/keycloak`
5.10.0, the latest) creates them (`keycloak_group.organization_id`) but has no resource for their
members, so `infra/keycloak` sets those through the Organization API (its README says how).

| Organization | Group ids (`owner`, `admin`, `member`, `viewer`) |
|---|---|
| `acme` | `9a010000-0000-4000-8000-00000000000{1,2,3,4}` |
| `globex` | `9a020000-0000-4000-8000-00000000000{1,2,3,4}` |
| `unlinked` | `9a030000-0000-4000-8000-00000000000{1,2,3,4}` |

### Events

User events and admin events are on, admin events with their representations
(`adminEventsDetailsEnabled`). Keycloak keeps both for 7 days (`eventsExpiration` 604800,
realm attribute `adminEventsExpiration` 604800), longer than the interval between reconciliation
passes, which repair what the event store let expire. The event types are Keycloak's defaults and
the listener is `jboss-logging`.

### The admin service account

`identity-stack-admin` pushes canonical changes to the realm and reads its events back. Its
service account holds the `realm-management` roles `manage-users`, `manage-organizations`,
`query-clients` and `view-events`, no realm role, and two fine-grained admin permissions, through
a user policy naming the service account: `view`, `manage` and `map-roles-composite` on the client
`identity-stack-api`, and `view` on the client `realm-management`. The import names both clients
by `clientId` in the permissions' `resources`; Keycloak resolves them to the clients' ids. Without `kc-admin-api` as a client scope its token lacks
the Admin API audience and every Admin API call is 401.

Probed on 26.8.2 against both the imported realm and the Terraform-built one:

| What it does | Needs | Observed |
|---|---|---|
| Find, create, update (disable) and delete users | `manage-users` | 200/201/204/204 |
| Find, create and delete organizations | `manage-organizations` | 200/201/204 |
| Add and remove an organization member | `manage-users` and `manage-organizations` | 201/204; with `manage-organizations` alone, adding is 403; with `manage-users` alone, listing and creating organizations are 403 |
| Create and delete an organization's group; add, list and remove its members | `manage-users` and `manage-organizations` | 201/204, 204/200/204; without `manage-users` the members calls fail (403/404) |
| Find the API client by `clientId` (`GET /clients?clientId=`) | `query-clients` and the permission's `view` | 200 with the client; without either, the lookup does not find it |
| List, create, update and delete the API client's roles | the permission's `view` and `manage` | 200/201/204/204; with `manage` alone, 403 |
| Add and remove a role's composites | the permission's `map-roles-composite` | 204/204; without it 403 |
| Read admin events and user events | `view-events` | 200/200 |
| Read its own `realm-management` roles (`GET /users/{id}/role-mappings/clients/{realm-management}/composite`), as the adapter does at startup | the `view` permission on `realm-management` | 200 with the four roles; without it Keycloak leaves `realm-management` out of the answer (`{}` and `[]`) |

Refused (403): reading or changing any other client (the SPA's redirect URIs among them), changing
the `realm-management` client, giving itself `realm-admin`, listing or creating realm roles,
changing realm settings. A realm-wide `manage-clients` would let it
rewrite the SPA client, so the client permission replaces it. What the grants still allow beyond
the adapter's needs, because Keycloak offers nothing narrower: `manage-users` also creates realm
groups (201), and `manage` on the API client also changes the API client's own settings and reads
its secret (204/200). The API client has no flow on, and a token it could get names it in `azp`,
not the SPA.

### Users

The realm brokers no identity provider, lets no one register, keeps emails unique and has
`verifyEmail` on, because the backend links a Keycloak identity to an existing canonical user
on a verified email ([`docs/security.md`](../../docs/security.md#authorization-rbac--fga)).
Every fixture user's email is imported as verified. No user holds a client role or a realm role.

| User | Id (`sub`) | Password | Organizations and role assignments |
|---|---|---|---|
| `alice` | `a11ce000-0000-4000-8000-000000000001` | `local-only-alice` | `acme`: `member` |
| `bob` | `b0b00000-0000-4000-8000-000000000002` | `local-only-bob` | `acme`: `admin`; `globex`: `viewer` |
| `carol` | `ca401000-0000-4000-8000-000000000003` | `local-only-carol` | `acme`: none |
| `dave` | `da4e0000-0000-4000-8000-000000000004` | `local-only-dave` | `acme`: `admin` |
| `erin` | `e4140000-0000-4000-8000-000000000005` | `local-only-erin` | `acme`: `member` |
| `frank` | `f4a4c000-0000-4000-8000-000000000006` | `local-only-frank` | `acme`: `admin` |
| `grace` | `64ace000-0000-4000-8000-000000000007` | `local-only-grace` | `unlinked`: `member` |

The users, the organizations, who belongs where and each one's role there are the shared test
fixture [`infra/fixtures/test-identities.json`](../../infra/fixtures/test-identities.json), which
every provider's Terraform root builds from; the unit test
`backend/tests/unit/test_identity_fixture.py` fails when this import disagrees with it. The ids
and passwords are Keycloak's own.

The login form is identity-first: username on one page, password on the next.

## Claim shapes

Observed on the recorded tokens and on every login `make keycloak-terraform-check` makes.

| Claim | Observed |
|---|---|
| `iss` | `<KC_HOSTNAME>/realms/identity-stack`, equal to `issuer` in discovery. No trailing slash. |
| `typ` | `Bearer` on access tokens, `ID` on ID tokens. Keycloak marks refresh tokens `Refresh`. Access tokens carry `sid` and `auth_time` too, so only `typ` tells them from ID tokens. |
| `aud` | SPA access token: the JSON **string** `"identity-stack-api"`, nothing else (no `account`), also for `carol`, who has no role assignment. SPA ID token: `"identity-stack-spa"`. `identity-stack-other`'s access token: **no `aud`**. |
| `azp` | The client logged in through: `identity-stack-spa` or `identity-stack-other`. |
| `email_verified` | `true`, on access and ID tokens. `email`, `name`, `given_name`, `family_name` and `preferred_username` are on both. |
| `organization` | A JSON **array of aliases**: `["acme"]`, on access and ID tokens. Absent when the `organization` scope is not requested. |
| `resource_access`, `realm_access` | **Absent** on every token, also for a user with role assignments (`bob`, `dave`): no role is mapped to a user or group, the clients have full scope off and no scope mapping, and the `roles` client scope is not on them. |
| `groups` | Absent. |
| `scope` | Space-separated string; includes the `organization` or `organization:<alias>` that was granted. Order varies between tokens. |

The `organization` claim can hold more than one alias. `organization:acme organization:globex`
and `organization:*` both gave `bob` `["globex", "acme"]`. Scope `organization` with no alias
gives a member of one organization that one (`alice`: `["acme"]`) and stops a member of several
on an organization picker page.

## Other things measured on 26.8.2

Measured on 2026-11-15 and 2026-11-16 with the earlier realm, whose clients differed; the
organization behaviour below does not depend on the clients.

| Row | Observed |
|---|---|
| Full-URL `KC_HOSTNAME` with `http://` | Works. Every endpoint in discovery uses that origin, and the same URL answers from the host and from other containers through the network alias (checked from the Debian-based backend image and from Alpine). |
| A refreshed token keeps its organization | Yes. A plain refresh returns `organization: ["acme"]` again. A refresh that asks for `scope=openid organization:globex` succeeds but returns a token with **no** `organization` claim, so refresh cannot switch organization. |
| Second authorize request on a live SSO session | For a member of both organizations, a second authorize request with `organization:globex` returns a code with no login form, also with `prompt=none`; the new token has `organization: ["globex"]` and the same `sid`. |
| A token after the user is removed from its organization | The access token stays valid until it expires (300 s); `userinfo` and the next refresh answer `400 invalid_grant` "Invalid organization". Added back, the same refresh token works again. |
| An organization the user is not in | Never put in a token, but not refused either: `organization:globex` for `alice` (in `acme` only) issues a token with no `organization` claim; with `organization:acme organization:globex` the `globex` scope is dropped; on a live session the password page is shown again, and with `prompt=none` the answer is `error=login_required`. `organization:nope` (no such organization) is `error=invalid_scope` before any login. A caller treats a missing `organization` claim, or one that differs from what it asked for, as a refusal. |
| Refresh tokens | The public clients get one in the standard flow (`expires_in` 300, `refresh_expires_in` 1800 with realm defaults). |
| PKCE | An authorize request without `code_challenge_method` is refused with `invalid_request`. |
| Cookies | Session cookies are set `Secure; SameSite=None` although the origin is plain HTTP. Chromium kept them for `keycloak.localhost`. `curl` with a cookie jar does not send them back, and the login form then answers 400. |
