# Keycloak fixtures

Taken on 2026-11-15 from `quay.io/keycloak/keycloak:26.8.2`, started with
`docker compose --profile keycloak up --wait` and the realm in
[`../realm/identity-stack-realm.json`](../realm/identity-stack-realm.json).

| File | What it is |
|---|---|
| `discovery.json` | Body of `GET http://keycloak.localhost:8180/realms/identity-stack/.well-known/openid-configuration`, fetched from the host. The backend container returned the same document. |
| `access-token.json` | Decoded header and claims of one access token: user `bob`, client `member-app`, scope `openid organization:acme`, authorization code flow with PKCE S256 through the login form. The signature is not stored. |

The signing key is generated per container, so `kid`, `sid`, `jti` and the times differ on every
instance. The claim names and shapes are what the fixture is for. The users' ids are fixed in the
realm import (table below), so a token's `sub` is the same on every instance from now on; the
`sub` in `access-token.json` was taken before that.

## The realm

[`infra/keycloak/`](../../infra/keycloak/README.md) builds the same realm in Terraform. A change
that adds a user, client, role, organization or member to the import file adds it there too;
`make keycloak-terraform-check` compares the two.

Every secret and password below is a local fixture. The realm's redirect URIs
(`http://localhost:3000`, post-logout `http://localhost:3000/login`), its SSL mode `none` and the
`admin`/`admin` bootstrap login are for this local container only; do not copy them elsewhere.

| Client | Kind | Client roles |
|---|---|---|
| `member` | confidential type client, no flows | `documents:read`, `documents:write` |
| `tenant-admin` | confidential type client, no flows | `members:invite`, `members:remove`, `settings:manage` |
| `member-app` | public, PKCE S256, full scope off, scope-mapped to `member` | none |
| `tenant-admin-app` | public, PKCE S256, full scope off, scope-mapped to `tenant-admin` | none |
| `identity-stack-directory` | confidential, service account only, client scope `kc-admin-api`, full scope off | its service account holds `realm-management` `view-users` and `view-organizations` |
| `identity-stack-admin` | confidential, service account only, client scope `kc-admin-api`, full scope off | its service account holds `realm-management` `manage-users` and `manage-organizations` |

`identity-stack-directory` is the backend's organization lookup (`KEYCLOAK_DIRECTORY_CLIENT_ID`,
secret `local-only-directory-client`). Probed on 26.8.2: the endpoint
`GET /admin/realms/identity-stack/organizations/members/{id}/organizations` opens to neither
role alone and to both together, and both are read-only. With them the service account is
refused (403) creating an organization, adding a member, editing an organization's attributes,
mapping a client role and listing clients. Without the `kc-admin-api` client scope its token
lacks the Admin API audience and every Admin API call is 401.

`identity-stack-admin` is the backend's Keycloak admin adapter (`KEYCLOAK_ADMIN_CLIENT_ID`,
secret `local-only-admin-client`), which pushes canonical tenants and users to the realm
(`backend/app/services/adapters/keycloak.py`). Probed on 26.8.2: `manage-users` and
`manage-organizations` together let it find, create, update and delete users and
organizations and add and remove organization members, and nothing else it needs; it is
refused (403) listing realm roles.

The realm brokers no identity provider, lets no one register, keeps emails unique and has
`verifyEmail` on, because the backend links a Keycloak identity to an existing canonical user
on a verified email ([`docs/security.md`](../../docs/security.md#authorization-rbac--fga)).
Every fixture user's email is imported as verified.

| User | Id (`sub`) | Password | Organizations | Client roles |
|---|---|---|---|---|
| `alice` | `a11ce000-0000-4000-8000-000000000001` | `local-only-alice` | `acme` | `member`: both |
| `bob` | `b0b00000-0000-4000-8000-000000000002` | `local-only-bob` | `acme`, `globex` | `member`: `documents:read`; `tenant-admin`: all three |
| `carol` | `ca401000-0000-4000-8000-000000000003` | `local-only-carol` | `acme` | none |
| `dave` | `da4e0000-0000-4000-8000-000000000004` | `local-only-dave` | `acme` | `tenant-admin`: all three |
| `erin` | `e4140000-0000-4000-8000-000000000005` | `local-only-erin` | `acme` | `member`: `documents:write` |
| `frank` | `f4a4c000-0000-4000-8000-000000000006` | `local-only-frank` | `acme` | `tenant-admin`: all three |
| `grace` | `64ace000-0000-4000-8000-000000000007` | `local-only-grace` | `unlinked` | `member`: both |

`dave` is a tenant admin in exactly one organization, and `erin` is a `member` user without
`documents:read`. `frank` is another tenant admin of `acme` alone, whom the integration tests
move to `globex` and back. `grace` is the one member of `unlinked`, an organization no canonical
tenant names.

The users, the organizations and who belongs where are the shared test fixture
[`infra/fixtures/test-identities.json`](../../infra/fixtures/test-identities.json), which every
provider's Terraform root builds from; the unit test `backend/tests/unit/test_identity_fixture.py` fails
when this import disagrees with it. The ids, passwords and client roles are Keycloak's own.

Organization `acme` carries the attribute `type:tenant-admin` listing `bob`, `dave` and `frank`:
the organization's record that they hold the `tenant-admin` type there. `globex` marks no one.
Client roles are realm-wide, so the backend believes an organization-bound token only where the
token's organization marks its user. The backend's token model for this realm declares `member` portable with
the one portable permission `documents:read`, and `tenant-admin` organization-bound
([`docs/architecture.md`](../../docs/architecture.md#keycloak-token-model)). `bob`'s
`tenant-admin` tokens are therefore refused in both of his organizations (he belongs to two),
`dave`'s and `frank`'s are accepted in `acme`, and `erin`'s `member` token grants nothing. Removing a member
from an organization leaves its marker alone: adding them back restores the type there unless the
id is taken off the attribute too.

The login form is identity-first: username on one page, password on the next.

## Claim shapes

All observed on access tokens from the running container.

| Claim | Observed |
|---|---|
| `iss` | `http://keycloak.localhost:8180/realms/identity-stack`. Equal to `issuer` in discovery, from the host and from the backend container. No trailing slash. |
| `aud` | A JSON **string**, not an array: `"member"` through `member-app`, `"tenant-admin"` through `tenant-admin-app`. Nothing else is in it (no `account`). |
| `azp` | Present; the app client (`"member-app"`). |
| `organization` | A JSON **array of aliases**: `["acme"]`. Not a map. |
| `resource_access` | `{"<type client>": {"roles": [...]}}`, holding only the type client the app client is scope-mapped to. Role order varies between tokens. |
| `scope` | Space-separated string that includes the `organization:<alias>` that was granted. Order varies between tokens. |
| No client role (`carol`) | The token has **no `aud` and no `resource_access`** at all. `azp` and `organization` are still there. |
| No organization scope requested | No `organization` claim. |
| ID token | `aud` is the app client (`"member-app"`); `organization` has the same array shape. |

The `organization` claim can hold more than one alias. `organization:acme organization:globex`
and `organization:*` both gave `bob` `["globex", "acme"]`. Scope `organization` with no alias
stops on an organization picker page.

## Rows of the epic's evidence table settled here

| Row | Observed |
|---|---|
| Default shape of the `organization` claim | Array of alias strings. |
| `resource_access` nesting | `resource_access.<client>.roles`, an array of strings. |
| `aud` is only the type client | Yes, as a single string. Absent when the user has no role on the type client. |
| `azp` on access tokens | Present, equal to the app client. |
| Issuer string | `<KC_HOSTNAME>/realms/<realm>`. |
| Full-URL `KC_HOSTNAME` with `http://` | Works. Every endpoint in discovery uses that origin, and the same URL answers from the host and from other containers through the `keycloak.localhost` network alias (checked from the Debian-based backend image and from Alpine). |
| A refreshed token keeps its organization | Yes. A plain refresh returns `organization: ["acme"]` again. A refresh that asks for `scope=openid organization:globex` succeeds but returns a token with **no** `organization` claim, so refresh cannot switch organization. |
| Second authorize request on a live SSO session | For a member of both organizations, a second authorize request with `organization:globex` returns a code with no login form, also with `prompt=none`. The new token has `organization: ["globex"]` and the same `sid`. The refresh token issued for `acme` keeps working and still yields `acme`. The end-to-end suite repeats this through the SPA's tenant switch on every pull request ([`docs/keycloak.md`](../../docs/keycloak.md)). |
| Listing a user's organizations: the admin role it needs | `realm-management` `view-users` **and** `view-organizations`, plus the `kc-admin-api` client scope on the calling client; neither role alone opens it (see `identity-stack-directory` above). |
| A token after the user is removed from its organization | The access token stays valid until it expires (300 s); `userinfo` and the next refresh answer `invalid_grant` "Invalid organization", so the `organization` claim does not survive a refresh. Measured below. |

## An organization the user is not in

Keycloak 26.8.2 never put an organization in a token for a user outside it, but it **did not
refuse the request** in the way the server administration guide describes. `alice` is in
`acme` only:

| Request | Result |
|---|---|
| `organization:globex`, fresh login | Code and token are issued. The token has no `organization` claim and `organization:globex` is missing from `scope`. |
| `organization:acme organization:globex`, fresh login | Token with `organization: ["acme"]`; the `globex` scope is dropped. |
| `organization:globex` on a live session started for `acme` | The password page is shown again. After it, the token has no `organization` claim. |
| The same with `prompt=none` | Redirect with `error=login_required`. |
| `organization:nope` (no such organization) | Redirect with `error=invalid_scope` before any login. |

A caller therefore has to treat a missing `organization` claim, or one that differs from what it
asked for, as a refusal.

## Other things seen

- The public app clients get a refresh token in the standard flow (`expires_in` 300,
  `refresh_expires_in` 1800 with realm defaults).
- An authorize request without `code_challenge_method` is refused with `invalid_request`.
- Session cookies are set `Secure; SameSite=None` although the origin is plain HTTP. Chromium
  kept them for `keycloak.localhost`. `curl` with a cookie jar does not send them back, and the
  login form then answers 400.

## Leaving an organization while holding a token for it

Measured on 2026-11-16 against `quay.io/keycloak/keycloak:26.8.2` with this realm and realm
default lifetimes (access token 300 s). `alice` logged in through `member-app` for `acme`
(token with `organization: ["acme"]`), then an admin removed her from `acme`:

| Step | Observed |
|---|---|
| The access token she holds | Unchanged and still valid until its `exp`: it is a JWT the backend verifies with the realm's keys, and nothing revokes it. A portable permission on it holds for the rest of its lifetime, at most 300 s. |
| `userinfo` with that access token | `400 invalid_grant`, "Invalid organization". |
| Refresh with her refresh token | `400 invalid_grant`, "Invalid organization". No new token is issued, so the `organization` claim does not survive the refresh. |
| She is added back to `acme`, same refresh token | `200`; the new token has `organization: ["acme"]` again. |

So the window after removal is the remaining lifetime of the access token already issued, and
it ends at the next refresh. For an organization-bound type the backend asks the Admin API on
every request, so the window there is closed by the lookup.
