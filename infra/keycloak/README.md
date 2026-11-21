# Keycloak realm in Terraform (`infra/keycloak`)

Terraform that builds the identity-stack realm in a Keycloak: the same realm the compose
services import from [`keycloak/realm/identity-stack-realm.json`](../../keycloak/realm/identity-stack-realm.json).
It uses the `keycloak/keycloak` provider (`~> 5.10`) and `hashicorp/random` (`~> 3.6`).

It targets the local compose container. There is no hosted Keycloak; adding one is an owner
decision recorded in the Keycloak epic. State is local (`terraform.tfstate` in this directory)
and git-ignored; `.terraform.lock.hcl` is committed.

## What it creates

The model: the token says who the caller is, and the canonical store (the backend database) says
what the caller may do. No role the realm holds reaches a token.

| Resource | What |
|---|---|
| `keycloak_realm.this` | The realm (default name `identity-stack-terraform`): Organizations on, registration off, unique emails, `verifyEmail` on, fine-grained admin permissions (v2) on, brute-force detection on with temporary lockouts (30 failures, wait growing from 60 s to 15 min), SSL mode `var.ssl_required` (`none` for the local container), admin events kept 7 days (realm attribute `adminEventsExpiration`). |
| `keycloak_realm_events.this` | User events and admin events on, admin events with representations, user events kept 7 days (`local.events_expiration_seconds`), listener `jboss-logging`. 7 days outlasts the interval between reconciliation passes. |
| `keycloak_openid_client.api` | `identity-stack-api`: confidential, no flows, full scope off. Its client id is the API audience. |
| `keycloak_role.permission`, `keycloak_role.role` | The API client's roles: the canonical permissions `<domain>:<action>` (`local.permissions`, twelve) and the canonical roles `owner`, `admin`, `member`, `viewer`, `operator` (`local.roles`); `operator` has no permissions, the others are composites of theirs. No user and no group is mapped to any of them. |
| `keycloak_openid_client.public` | `identity-stack-spa` (the SPA) and `identity-stack-other` (another client, for tests that its tokens are refused): public, standard flow only, PKCE `S256`, full scope off, redirect URI and web origin `var.frontend_url`, post-logout redirect `<frontend_url>/login`. |
| `keycloak_openid_client_default_scopes.public`, `keycloak_openid_client_optional_scopes.public` | Their client scopes: default `basic`, `profile`, `email`, `web-origins`, `acr` (no `roles`, so no token carries `resource_access` or `realm_access`); optional `organization`, `offline_access`. |
| `keycloak_openid_audience_protocol_mapper.spa_api_audience` | Puts `identity-stack-api` in the SPA's access tokens' `aud` (not its ID tokens'). `identity-stack-other` has no such mapper. |
| `keycloak_user.this`, `keycloak_user_roles.this`, `random_password.user` | The shared fixture users ([`../fixtures/test-identities.json`](../fixtures/README.md)), `alice` to `grace`, and Keycloak-only `helen` from [`test-identities.json`](test-identities.json), email `<user>@<home tenant's domain>`, the fixture's names. `keycloak_user_roles` is exhaustive and empty: no user holds a role, as in the import. |
| `keycloak_organization.this`, `keycloak_organization_memberships.this` | One organization per fixture tenant: `acme` (domain `acme.test`), `globex` (`globex.test`) and `unlinked` (`unlinked.test`), with each fixture user in each tenant their fixture roles name. `acme` has seven users, `globex` has `bob`, `unlinked` has `grace`. |
| `keycloak_group.role` | Role assignments: five groups owned by each organization (`organization_id`), one per canonical role, mapping no role. |
| `terraform_data.role_assignment` | Each fixture user's role in each tenant, as membership of that organization's group for the role (`local.role_assignments`). |
| `keycloak_openid_client.admin` and its scopes, roles and scope mappings | `identity-stack-admin`, the backend's service account for the Keycloak admin adapter: client scope `kc-admin-api`; its service account holds exactly `realm-management` roles `manage-users`, `manage-organizations`, `query-clients` and `view-events` and no realm role (`keycloak_user_roles.admin`, exhaustive), as in the import. |
| `keycloak_openid_client_user_policy.admin`, `keycloak_openid_client_admin_permissions.admin_api_client`, `keycloak_openid_client_admin_permissions.admin_realm_management` | Its fine-grained admin permissions: `view`, `manage` and `map-roles-composite` on `identity-stack-api`, and `view` on `realm-management` (so it can read its own roles back). Each grant is probed and recorded in [`keycloak/fixtures/README.md`](../../keycloak/fixtures/README.md#the-admin-service-account). |

The root declares one protocol mapper, the audience mapper above. Keycloak's built-in client scopes
write `organization` from memberships, and Keycloak sets `azp` itself; no mapper copies a user
attribute or property into `aud`, `azp`, `organization`, `resource_access`, `dct` or `tenants`.

Two things are done outside the provider, with `bash`, `curl` and `jq` and the bootstrap admin login
the provider uses (`KEYCLOAK_USER`, `KEYCLOAK_PASSWORD`):

- **Organization group members.** Keycloak manages an organization's groups through the
  Organization API only, and the provider has no resource for their members. Each
  `terraform_data.role_assignment` adds its user with
  `PUT /admin/realms/{realm}/organizations/{id}/groups/{group}/members/{user}` when created and
  removes it on destroy. A changed user, group or organization id replaces it, so the membership
  is set again. Unlike the other resources, it does not see a member added or removed by hand:
  `make keycloak-terraform-check` does. A user replaced in an apply (`-replace`, or deleted by
  hand) also leaves their organization, which `keycloak_organization_memberships` restores only
  on the next plan, so the first apply fails on that user's assignment (400, not a member of the
  organization) and a second apply completes it.
- **Secrets on the command line.** The admin password and the Admin API token reach `curl` on
  stdin (`--data-urlencode password@-`, `-H @-`), never as arguments another process could read.
- **The admin-permissions client's id.** The provider's `keycloak_openid_client` data source also
  reads a client's secret, which Keycloak 26.8 refuses for `admin-permissions` (403), so
  `data.external.admin_permissions_client` reads the id from the realm (`adminPermissionsClient`).

Two things differ from the import, by necessity:

- **User and group ids.** The provider cannot set a user's id, so Keycloak picks them and a
  token's `sub` is not the import's fixed id. The output `user_ids` lists them. The same holds for
  the organization groups' ids, which the import also fixes.
- **Credentials.** The import's fixture passwords and client secrets are not repeated here.
  User passwords are generated (`random_password`) and the admin client's secret is generated by
  Keycloak. Both are sensitive outputs, kept in local state only. The API client's secret is never
  used (it has no flows) and is not output.

## Run it

Start the compose Keycloak, then apply. The provider logs in to the `master` realm as the
bootstrap admin through `admin-cli`; the login comes from the environment, never from a tfvars
file. `admin`/`admin` is the compose container's local fixture login.

The realm must not exist before the apply. The compose `keycloak` service already imports a realm
named `identity-stack`, so the root's default realm is `identity-stack-terraform`, which sits
beside it. Against a Keycloak that did not import the realm,
`-var issuer_url=http://keycloak.localhost:8180/realms/identity-stack` builds it under the
import's name.

```bash
docker compose --profile keycloak up -d --wait keycloak
cd infra/keycloak
export KEYCLOAK_USER=admin KEYCLOAK_PASSWORD=admin
terraform init
terraform apply
cd ../.. && make -s keycloak-env    # backend/.env and frontend/.env lines
terraform -chdir=infra/keycloak output -json test_credentials
```

The root has one local state, so it manages one Keycloak at a time. Changing `issuer_url` to
another instance (for example `keycloak-test`, which the end-to-end suite uses, see
[`docs/keycloak.md`](../../docs/keycloak.md#running-the-end-to-end-suite-locally)) replaces the
realm in state with one on the new instance and leaves the first instance's realm unmanaged:
the next apply against it then fails because the realm already exists. Before switching, destroy
what the state holds, with the `issuer_url` it was applied with:

```bash
terraform -chdir=infra/keycloak destroy    # add -var issuer_url=... if the apply had one
```

Variables (all have defaults for the compose `keycloak` service). `issuer_url` and
`frontend_url` are the variables every local provider root shares
([`../PROVIDERS.md`](../PROVIDERS.md#common-variables-and-outputs-of-the-local-provider-roots)):

| Variable | Default | What |
|---|---|---|
| `issuer_url` | `http://keycloak.localhost:8180/realms/identity-stack-terraform` | The realm's issuer, `<KC_HOSTNAME>/realms/<realm>`: the provider logs in to `<KC_HOSTNAME>` and creates `<realm>`, which must not exist yet. Not `identity-stack` beside the compose import, which already has that name. `http://` only for a loopback or `*.localhost` host (validated). |
| `frontend_url` | `http://localhost:3000` | SPA origin: redirect URI, web origin, post-logout `<frontend_url>/login`. A bare origin, `scheme://host[:port]`: no path, query or trailing slash; `http://` only for a loopback or `*.localhost` host (both validated). |
| `ssl_required` | `none` | Realm SSL mode. `none` only when `issuer_url` is a loopback or `*.localhost` host (validated); set `external` or `all` for any other Keycloak. |

## Outputs

| Output | Feeds |
|---|---|
| `issuer_url` | The realm's issuer, the `iss` of its tokens. |
| `api_audience` | `identity-stack-api`, the audience the SPA's access tokens carry. |
| `spa_client_id` | `identity-stack-spa`, the `azp` of the SPA's tokens. |
| `other_client_id` | `identity-stack-other`, whose tokens lack the API audience. |
| `organization_aliases` | Each fixture tenant's organization alias, the value of the token's `organization` claim. |
| `user_ids` | Each user's id (token `sub`). |
| `backend_environment` (sensitive) | Every backend Keycloak setting: `KEYCLOAK_ISSUER_URL`, `KEYCLOAK_AUDIENCE`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_REQUIRE_HTTPS`, `KEYCLOAK_POST_LOGOUT_REDIRECT_URI`, `KEYCLOAK_ADMIN_CLIENT_ID`, `KEYCLOAK_ADMIN_CLIENT_SECRET` ([`docs/environment-variables.md`](../../docs/environment-variables.md)). Sensitive for the secret. |
| `frontend_environment` | Frontend `VITE_OIDC_AUTHORITY`, `VITE_OIDC_CLIENT_ID`, `VITE_OIDC_REDIRECT_URI`, `VITE_OIDC_SCOPE` for the SPA's client. The scope is `openid profile email organization`: `organization` is optional on the client, and without it the token carries no `organization` claim. Keycloak asks a member of several organizations which one to use. |
| `test_credentials` (sensitive) | `users`: each fixture user's login password. `clients`: empty, no client is for tests only. |

The Zitadel root gives the same names where the providers agree (`issuer_url`, `spa_client_id`,
`other_client_id`); its audience is the project (`project_id`) and its organizations are ids
(`organization_ids`).

## Members and role assignments added by hand

`keycloak_organization_memberships` is authoritative over an organization's members. A member
added to `acme` or `globex` in the admin console or through the Admin API shows up as a change in
the next `terraform plan`, and the next `terraform apply` removes them. Add members here instead.
`keycloak_user_roles` is authoritative in the same way over each user's roles. Organization group
members are not (see above): `terraform plan` does not show one added or removed by hand.

## Keeping it the same as the import

When a change adds a user, client, role, organization or member to the import file, the same
change adds it here. `make keycloak-terraform-check`
([`scripts/check-against-compose.sh`](scripts/check-against-compose.sh)) checks the two against
each other in the compose `keycloak-test` container. The `identity-stack` realm there is the
import from when the container was created; after a change to
[`keycloak/realm/identity-stack-realm.json`](../../keycloak/realm/identity-stack-realm.json),
recreate it with `make test-down && make test-up-keycloak`, as a failed comparison says. The check:

1. deletes any `identity-stack-terraform` realm an earlier killed or failed run left behind,
   then applies this root to a new realm of that name beside
   the imported `identity-stack`, with its own state in a new directory under
   `${XDG_CACHE_HOME:-~/.cache}/identity-stack-keycloak-terraform-check/`;
2. runs `terraform plan` again and fails unless it is empty;
3. runs [`scripts/check_realm.py`](scripts/check_realm.py), which compares, in both
   directions, the realm and event settings, every client (Keycloak's built-in ones by name only)
   with its protocol mappers, the API client's roles, their composites and who holds them, the
   admin service account's roles and fine-grained admin permission, every user and their roles
   and realm groups, organizations, domains, members, attributes and organization groups with
   their members and role mappings, through the Admin API; logs users in to both realms and
   compares the tokens' `iss`, `typ`, `aud`, `azp`, `organization`, `resource_access`,
   `realm_access`, `email_verified`, `scope` and claim names; fails unless the SPA's access
   tokens are `Bearer` with the API audience and no `resource_access` or `realm_access`, its ID
   tokens are `ID`, the other client's tokens lack the API audience, and no user or group holds
   an API client role; compares the claim names with the recorded tokens in
   [`keycloak/fixtures/`](../../keycloak/fixtures/README.md); fails on any protocol mapper that
   writes a user attribute or property to `aud`, `azp`, `organization`, `resource_access`, `dct`
   or `tenants`; checks every output against the realm and logs in with the output
   `VITE_OIDC_SCOPE`; and checks the admin client's generated secret against the Admin API (it
   reads organizations, events and the API client's roles, and is refused the SPA client). Users,
   organizations and members are read one page of `LISTING_PAGE_SIZE` at a time, and a full page
   fails the check as possibly cut off;
4. with `--integration-tests` (`make keycloak-terraform-integration`), runs the backend's
   Keycloak integration tests (`backend/tests/integration/test_keycloak_*.py`) against the
   built realm, logging in with the generated user ids and passwords and asking the admin
   adapter with its generated secret (`backend/tests/helpers/keycloak_target.py`). They need the rest of
   the test stack up and, for one test, a Descope client (`DESCOPE_PROJECT_ID`,
   `DESCOPE_CLIENT_ID`, `DESCOPE_CLIENT_SECRET`, read from `backend/.env` when present);
5. destroys the realm. The run's state directory holds the generated passwords and client
   secrets and is deleted, except when the destroy failed: then it is kept and its path printed.
   The next run deletes the realm (step 1) and then every state directory an earlier run kept
   or was killed with.

The comparison covers the fields the import file uses today, listed at the top of
`check_realm.py` (`REALM_SETTINGS`, `REALM_ATTRIBUTES`, `EVENT_SETTINGS`, `CLIENT_SETTINGS`,
`CLIENT_ATTRIBUTES`, `MAPPER_CONFIG`, `USER_SETTINGS`). A change that makes the import use a field
outside those lists (a user attribute, a required action, another client attribute or mapper
setting, a realm role) adds the field to the list in the same change; the check does not notice an uncompared field by itself.

It copies only the root's `.tf` files git does not ignore into its working directory, so a local
`override.tf` or `*_override.tf` never changes what it applies.

It needs `bash`, `terraform`, `curl`, `jq` and the backend virtual environment
(`cd backend && uv sync --locked --extra dev`), and checks for each before it does anything.
Neither the script nor `make keycloak-terraform-check` starts, stops or removes keycloak-test:
bring it up first with `make test-up-keycloak`; the script fails when it is not answering. It runs on Linux. CI
runs it on every pull request (the `keycloak-terraform-check` job in `.github/workflows/ci.yml`),
so a change to the import file without the matching change here fails the pull request. Two runs against one keycloak-test at the same time are not supported: where several
sessions share a Docker host, serialise them (for example with `flock`).
