# Keycloak fixtures

Taken on 2026-11-15 from `quay.io/keycloak/keycloak:26.8.2`, started with
`docker compose --profile keycloak up --wait` and the realm in
[`../realm/identity-stack-realm.json`](../realm/identity-stack-realm.json).

| File | What it is |
|---|---|
| `discovery.json` | Body of `GET http://keycloak.localhost:8180/realms/identity-stack/.well-known/openid-configuration`, fetched from the host. The backend container returned the same document. |
| `access-token.json` | Decoded header and claims of one access token: user `bob`, client `member-app`, scope `openid organization:acme`, authorization code flow with PKCE S256 through the login form. The signature is not stored. |

The signing key is generated per container, so `kid`, `sub`, `sid`, `jti` and the times differ on
every instance. The claim names and shapes are what the fixture is for.

## The realm

Every secret and password below is a local fixture. The realm's redirect URIs
(`http://localhost:3000`, post-logout `http://localhost:3000/login`), its SSL mode `none` and the
`admin`/`admin` bootstrap login are for this local container only; do not copy them elsewhere.

| Client | Kind | Client roles |
|---|---|---|
| `member` | confidential type client, no flows | `documents:read`, `documents:write` |
| `tenant-admin` | confidential type client, no flows | `members:invite`, `members:remove`, `settings:manage` |
| `member-app` | public, PKCE S256, full scope off, scope-mapped to `member` | none |
| `tenant-admin-app` | public, PKCE S256, full scope off, scope-mapped to `tenant-admin` | none |

| User | Password | Organizations | Client roles |
|---|---|---|---|
| `alice` | `local-only-alice` | `acme` | `member`: both |
| `bob` | `local-only-bob` | `acme`, `globex` | `member`: `documents:read`; `tenant-admin`: all three |
| `carol` | `local-only-carol` | `acme` | none |
| `dave` | `local-only-dave` | `acme` | `tenant-admin`: all three |
| `erin` | `local-only-erin` | `acme` | `member`: `documents:write` |

`dave` is a tenant admin in exactly one organization, and `erin` is a `member` user without
`documents:read`. The backend's token model for this realm declares `member` portable with
the one portable permission `documents:read`, and `tenant-admin` organization-bound
([`docs/architecture.md`](../../docs/architecture.md#keycloak-token-model)). `bob`'s
`tenant-admin` tokens are therefore refused in both of his organizations, and `erin`'s
`member` token grants nothing.

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
| Second authorize request on a live SSO session | For a member of both organizations, a second authorize request with `organization:globex` returns a code with no login form, also with `prompt=none`. The new token has `organization: ["globex"]` and the same `sid`. The refresh token issued for `acme` keeps working and still yields `acme`. |

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
