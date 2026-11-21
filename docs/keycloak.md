# Keycloak: what is proved, and its limits

identity-stack works against a Keycloak realm with Organizations as tenants, its principals
authorized from the canonical model ([ADR-0011](adr/0011-keycloak-canonical-authorization.md),
[architecture.md](architecture.md#keycloak-principals)). This page records what the automated
runs prove, the Keycloak version they ran against, and every limit found. Running Keycloak
locally and configuring the backend for it: [local-development.md](local-development.md#local-keycloak).

Tested against `quay.io/keycloak/keycloak:26.8.2`, the image `docker-compose.test.yml` pins,
with the realm imported from [`keycloak/realm/identity-stack-realm.json`](../keycloak/realm/identity-stack-realm.json)
and with the same realm built by [`infra/keycloak/`](../infra/keycloak/README.md). The realm's
users, clients and organizations are listed in
[`keycloak/fixtures/README.md`](../keycloak/fixtures/README.md).

## What runs, and where

| Run | What it proves | CI job |
|---|---|---|
| `backend/tests/e2e_keycloak/` | The shipped SPA and backend against the realm `infra/keycloak/` builds in `keycloak-test`, configured from the root's outputs, driven from Chromium, with the backend syncing to the realm (`SYNC_PROVIDER=keycloak`, the [event pull](#event-pull) on). No credential. | `E2E Tests (Keycloak)`, every pull request |
| `backend/tests/e2e/` with `E2E_PROVIDER=keycloak` | The shared UI and API suite ([local-development.md](local-development.md#end-to-end-suites)) against the same stack. Both principals are `bob`, signed in through `identity-stack-spa`: the admin acts in `acme`, where he is `admin`, the member in `globex`, where he is `viewer`, each naming its tenant in `X-Tenant-ID`, as the Zitadel run does. The tests that do not apply to Keycloak are listed with the reason in `backend/tests/e2e/not_applicable.py` | `E2E Tests (Keycloak)`, every pull request |
| `backend/tests/integration/test_keycloak_*.py` against the import | Token binding, first sign-in and canonical authorization in-process; every push of the [admin adapter](#admin-adapter) and its startup credential check; the [event pull](#event-pull) and [reconciliation](#reconciliation) against the realm | `Integration Tests` |
| The same tests against the Terraform-built realm (`make keycloak-terraform-integration`) | `infra/keycloak/` builds a realm the backend accepts the same way: generated user ids, passwords and admin secret, same results | `Integration Tests` |
| `make keycloak-terraform-check` | Every user, client, client role, organization, role group and member of the import is in the Terraform root and the other way round, and both realms issue tokens of the same shape | `Keycloak Terraform Check`, every pull request |

Both suites start by bringing the canonical model to the realm's state. `make keycloak-seed` links
the canonical tenants to the realm's organizations and the fixture users who hold a role in them
to their Keycloak accounts (`idp_links`), and assigns no role. The suites' session set-up
(`backend/tests/e2e_keycloak/realm.py`) then runs [reconciliation](#reconciliation) passes until
one applies nothing: the first takes the API client's permissions and roles and the members' role
groups in, and the second the role groups whose roles the first created (see
[Limits found](#limits-found)). A pass that fails, or passes that keep changing something, fail the
run.

The Keycloak suite asks for each of these:

1. `bob`, admin of `acme` and viewer of `globex`, signs in from the SPA with the plain
   `organization` scope. Keycloak asks for his username, then which organization (he is in two),
   then his password. The access token names the SPA as `azp`, the API in `aud` and the
   organization he picked, and carries no `resource_access`.
2. `GET /api/identity` returns him, his `keycloak` link and both tenants.
3. With the same token, `GET /api/access-checks/settings-manage` answers in `acme` and is refused
   (403) in `globex`, where `GET /api/access-checks/documents-read` answers: the grants are the
   ones Postgres holds in the tenant `X-Tenant-ID` names.
4. He switches tenant in the SPA's switcher, to the other tenant and back. The next API calls
   name the chosen tenant, no request goes to Keycloak, and the SPA holds the same token.
5. He signs out from the user menu. Keycloak no longer lists the session, the SPA holds no token,
   and Sign In shows Keycloak's login form.
6. `bob` through `identity-stack-other`, a client whose tokens lack the API audience: every route
   in the backend's OpenAPI document that needs a token answers 401 with the one rejection body.
7. `frank`, admin of `acme` only, is added in Keycloak to `globex` and its `admin` group. After the
   next poll he holds `settings:manage` in both tenants with the same token. Taken out of `globex`
   again, he is refused there after the next poll and still served in `acme`.
8. `erin`'s account is disabled in Keycloak. After the next poll her unexpired token is refused
   (403, `No tenant context`: a deactivated user holds no tenant) and `GET /api/identity` shows her
   `inactive`; enabled again, she is served again after the next poll.
9. `alice`, a member of `acme` only, naming `globex` in `X-Tenant-ID`, is refused there (403).

A missing `keycloak-test`, backend or SPA fails every test of the suite; it never skips.

## Running the end-to-end suite locally

The realm's app clients redirect only to `http://localhost:3000`, so the SPA must be served
there. The suite runs against the realm `infra/keycloak/` builds beside the import, and every
step below takes its settings from that root's outputs
([`infra/PROVIDERS.md`](../infra/PROVIDERS.md#common-variables-and-outputs-of-the-local-provider-roots)). With the test stack
up (`make test-up-keycloak`) and `keycloak-test.localhost` resolving to loopback, from the
repository root:

`infra/keycloak/` has one local state. If it holds the development apply against the compose
`keycloak` ([`infra/keycloak/README.md`](../infra/keycloak/README.md#run-it)), run
`terraform -chdir=infra/keycloak destroy` first; otherwise the apply below moves the state to
`keycloak-test` and the next development apply fails because its realm already exists. Destroy
this one the same way, with the same `-var issuer_url`, before going back. `make keycloak-env`
and `make test-e2e-keycloak` read whichever instance was applied last.

```bash
KEYCLOAK_USER=admin KEYCLOAK_PASSWORD=admin terraform -chdir=infra/keycloak apply \
  -var issuer_url=http://keycloak-test.localhost:18180/realms/identity-stack-terraform
make keycloak-env   # the backend/.env and frontend/.env lines
make test-up-openfga  # the relationship store: SYNC_PROVIDER=keycloak defaults to OpenFGA
terraform -chdir=infra/openfga init && terraform -chdir=infra/openfga apply -var api_url=http://localhost:18380
terraform -chdir=infra/openfga output backend_environment   # RELATIONSHIP_STORE and OPENFGA_* lines

export DATABASE_URL=postgresql+asyncpg://identity_test:identity_test@localhost:15432/identity_test
(cd backend && python -m alembic upgrade head)
make keycloak-seed  # the provider row, the tenants' and the fixture users' links
# then start the backend on :8000 (from backend/) with every backend line, the
# KEYCLOAK_ADMIN_CLIENT_* pair included, the infra/openfga lines, and SYNC_PROVIDER=keycloak (the suites need the event
# pull and reconciliation), CANONICAL_RBAC_PROVIDERS=keycloak, JIT_ENABLED_PROVIDERS=keycloak,
# DESCOPE_PROJECT_ID= DESCOPE_MANAGEMENT_KEY=, plus RATE_LIMIT_DEFAULT=2000/minute
# RATE_LIMIT_AUTH=1000/minute: the suite's refused requests share one client address, and the
# 60/minute default would answer some of them 429. KEYCLOAK_PULL_INTERVAL_SECONDS=2 makes the
# tests that wait for a poll quicker (CI keeps the default, 10). Set INTERNAL_IDENTITY_KEY,
# DESCOPE_WEBHOOK_SECRET and DESCOPE_FLOW_SYNC_SECRET to throwaway values, exported the same in
# the shell that runs the suite: the suite runs reconciliation with the last one.

cd frontend
# the frontend lines of `make keycloak-env` (the plain organization scope; the tenant is
# switched in the app)
export VITE_OIDC_AUTHORITY=... VITE_OIDC_CLIENT_ID=... VITE_OIDC_REDIRECT_URI=... VITE_OIDC_SCOPE=...
npm run build
npx vite preview --port 3000 --strictPort

make test-e2e-keycloak   # from the repository root; the shared suite, then backend/tests/e2e_keycloak/;
                         # reads the realm, user ids and passwords from the outputs
```

The suites change the realm only for the length of a test (an organization membership, an
account's `enabled`) and put it back; a run that is killed mid-test can leave such a change, which
`make test-down && make test-up-keycloak` and a new apply clear.

`E2E_BACKEND_URL` points the suite's API calls at another backend address; the SPA's own `/api`
calls go through the preview server's proxy to `localhost:8000`. The CI job
`E2E Tests (Keycloak)` in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs these
steps.

## Admin adapter

With `SYNC_PROVIDER=keycloak` the canonical services push every entity Keycloak can hold to the
realm through `KeycloakSyncAdapter` (`backend/app/services/adapters/keycloak.py`) instead of
Descope. It writes through the Admin REST API with one service account,
`KEYCLOAK_ADMIN_CLIENT_ID`/`KEYCLOAK_ADMIN_CLIENT_SECRET` (`identity-stack-admin` in the compose
realm), on one HTTP client with a 5-second timeout. The service account holds the
`realm-management` roles `manage-users`, `manage-organizations`, `query-clients`,
`view-events` and `create-client`, and the fine-grained admin permissions `view`, `manage` and `map-roles-composite` on
the API client (`KEYCLOAK_AUDIENCE`) alone; [`keycloak/fixtures/README.md`](../keycloak/fixtures/README.md#the-admin-service-account)
records what each grant allows and refuses. Startup takes a token, makes one Admin API read with it,
reads the service account's own `realm-management` roles (composites expanded) and looks the API
client up, and stops the backend before it serves a request when a setting is missing, Keycloak
refuses the credential, a role is missing, or the API client cannot be found or its roles managed
([environment-variables.md](environment-variables.md)). Map the roles to the service account
directly: roles it reaches only through a group or a composite realm role are not seen. No route
changes: the services that synced to Descope sync to Keycloak instead. With
`SYNC_PROVIDER=keycloak`, reconciliation (`POST /api/internal/reconciliation/run`) runs Keycloak's
pass ([Reconciliation](#reconciliation)).

| Canonical | Keycloak | What the adapter pushes |
|---|---|---|
| Tenant | Organization | `sync_tenant` creates the tenant's organization, or updates it: name, domains exactly the tenant's `self_provisioning_domains` when given (`[]` clears them), and `enabled` from the tenant's `status` when given (`active` enabled, `suspended` disabled). The tenant's organization is the one the tenant is linked to (`tenants.external_org_id` of the `keycloak` provider, as `make keycloak-seed` links the realm's fixture organizations), else the one whose alias is the canonical tenant id. One it creates is then linked to the tenant, so a first sign-in through it finds the tenant; a tenant linked to another provider's organization is refused. Keycloak resets the fields an update leaves out, so the organization is sent back as found with only those fields replaced, and not at all when it already matches. `delete_tenant` deletes the organization; one not there counts as deleted. |
| User | User | `sync_user` creates or updates the user's [Keycloak account](#which-keycloak-account): with none, the user whose username is the canonical user id, with the canonical email and the given and family names (`firstName`, `lastName`). A user created in a tenant (`UserService.create_user`) is then linked to it (`idp_links`, `keycloak`, `{"linked_by": "backend"}`), so its first sign-in finds it. A new user is enabled unless its status is `inactive`; a profile update does not send `enabled`, so a realm admin's enable or disable stands, while activate and deactivate (`STATUS_CHANGED`) push it. A changed email is set unverified (`emailVerified: false`): the realm links identities across providers by verified email. A status change for a user Keycloak lacks is an error and creates no account. `delete_user` deletes the account under the canonical id; one not there counts as deleted. |
| Membership | Organization member | A role assignment makes the user a member of the tenant's organization (below). The membership ends when the user's last role in the tenant is unassigned (`LAST_ROLE_IN_TENANT`), when a role deleted was their last there, or when the user is removed from the tenant (`remove_user_from_tenant`, before any profile update, so a refused update still ends it; it sends no `enabled`). Ending it takes the user out of every group of the organization too. A user Keycloak lacks is an error, and no account is created. |
| Permission | Client role `<domain>:<action>` of the API client | `sync_permission` creates or updates it (name, description), with the permission's id in the attribute `identity-stack.permission-id`; a rename (`previous_name`) renames it. `delete_permission` deletes it, and Keycloak drops it from every role's composites. |
| Role | Composite client role of the API client | `sync_role` creates or updates it, with the role's id in `identity-stack.role-id`, and, when the role's permissions are given (`permission_names`), sets its composites to exactly their client roles; a permission not pushed yet is an error. A global role's client role is named as the role, a tenant's own role `<name>@<tenant id>`, so two tenants' roles never share a name. A rename renames the role's groups in every organization (a tenant's own role: in its tenant's), then the client role. `delete_role` deletes the role's groups, then its client role. |
| Role assignment | Membership of the organization's group for the role | `sync_role_assignment` makes the user a member of the tenant's organization and of the organization's group named as the role's client role (created, with `identity-stack.role-id`, when the organization has none). The group maps no role, so the membership records the assignment and grants nothing a token carries. Unassigning takes the user out of the group; the last role in the tenant ends the membership. The tenant, the user and the role must have been pushed already. |
| Custom attributes (`custom_attributes` capability) | The caller's user attributes; the tenant's organization attributes | `GET /api/profile`, `PATCH /api/profile/attributes` and `/api/tenants/current/settings`, for a Keycloak principal only. Each value is one JSON string; a write reads the account or organization and sends it back at once with only the attributes changed (Keycloak replaces what a PUT sends), and leaves a user's `enabled`, `emailVerified` and `requiredActions` out, which Keycloak keeps. A change Keycloak takes between that read and write can still be overwritten. The realm's user profile has `unmanagedAttributePolicy` `ADMIN_EDIT`, so only admins (the adapter) see and set them. |
| Own names (`PATCH /api/profile`) | `firstName` and `lastName` of the caller's own account | For a Keycloak principal, `UserService.update_own_names` sends `OWN_PROFILE_OF` (the caller's `sub`): `sync_user` sets the names on that account, even one the user signed up with, and nothing else. A push that fails is answered `provider_sync: failed`, and the next pull restores the account's names. |

A client role or group is found by the canonical id it carries, else by its name when it carries
none: the realm import's permissions, roles and groups carry none, and the first push adopts them
(stamps the id; a rename stamps the client role before it renames anything). For a role assignment
that name is the canonical role's own, with its scope, read from the canonical store, so a tenant's
own role never falls back to a global role of the same name. One of that name that carries another
canonical id is refused. A group a concurrent assignment created first is used, not refused. Nothing the adapter
does maps a client role to a user or a group: the realm check `nothing_grants_api_roles` holds, and
a token issued after any push carries no `resource_access` or `realm_access`.

`RoleService` assigns a role only to a user who already holds one in the tenant, and refuses
anyone else (403, nothing written): a tenant admin cannot make a user of another tenant a member of
their organization. A user's first membership comes from signing in (just-in-time provisioning puts
them in the organization their token names), as
[identity-stack#592](https://github.com/jamescrowley321/identity-stack/issues/592) has it for every
provider. `RoleService` marks the unassignment that leaves no role in the tenant, counting the roles
left after its commit, and when a role is deleted it locks the role's row, reads who holds it, and
after its commit ends the membership of each user the role was the last of. When `RoleService` or
`UserService` finds, after a membership ended, that the user was given a role in the tenant
meanwhile, it pushes each of those assignments again, which adds back the membership and the
role groups the ending took the user out of.

Every push is idempotent. It reads what Keycloak holds first and writes only what differs, and the
answers Keycloak gives a write already made (a member already added or not there, a group or role
already gone) count as done, so a repeat writes nothing and a retry or a reconciliation re-push is
safe. The live suite checks each push of the table above twice and finds no admin event of the
admin client from the second. Two pushes of the same entity at once are not ordered: each sends a
complete canonical state, and the last to arrive stands; a realm admin's edit to a field the
adapter does not own (an organization's description) made between the adapter's read and its write
is overwritten.

Keycloak answers a missing organization membership with its generic 404 (`HTTP 404 Not Found`),
the answer a route that does not match gets too. Ending a membership counts that 404 as done only
once the user's organizations are read (`GET /organizations/members/{id}/organizations`) and the
organization is not among them, or the realm has no such user; otherwise it is an error.

Every failure comes back to the service as an error (`SyncError`), with the HTTP status, the
adapter's own refusal (`Refusal`) when it refused the push itself, and `unanswered` when Keycloak
gave no readable answer; it is logged, and the canonical change stands, as with Descope. Nothing
raises. A failure of the service's own database during a push (reading or writing a canonical
link) is such an error too, with no status: Keycloak was not asked. A push that takes access away
(ending a membership, a deactivation) and fails is logged as an `ERROR` beginning
`IdP revocation failed`, and the service's answer carries `"provider_sync": "failed"`: Keycloak
still holds what the canonical change took away until the push is made again: the change stays
[pending](#reconciliation), and the next reconciliation pass pushes it, ending the membership or
disabling the account, and keeps it pending until a push is delivered. Authorization
never reads it: a removed membership is refused on the next request from the canonical model.
`DELETE /api/roles/{name}` returns that answer; `POST /api/roles/remove` builds its own and does not
show it, so alert on the log. The unit tests replay answers recorded from Keycloak 26.8.2
([`backend/tests/fixtures/keycloak_admin/`](../backend/tests/fixtures/keycloak_admin/README.md)).

### Which Keycloak account

The adapter creates users under the canonical user id (their username). A user who signed in
through Keycloak first has their own account, linked to the canonical user by a `keycloak` IdP
link whose subject is the account's id. The adapter follows a link a sign-in or the backend made,
as the Zitadel adapter does: just-in-time provisioning records `{"linked_by": "sign_in"}` on the
link, from the validated token's subject; the adapter (for an account it created) and
`make keycloak-seed` (`seed_keycloak --link-users`, for an existing account of the realm) record
`{"linked_by": "backend"}`. `POST /api/users/{id}/idp-links` refuses a link that sets that key
(422). A link a tenant admin made names whatever account the admin typed in, so the adapter never
writes to it; nor to a client's service account (`service-account-*`, or one with a
`serviceAccountClientId`), whatever link names it. Links made before this rule carry no origin and
are not followed either.

Memberships (added and ended), group memberships, status changes and profile pushes reach the
account the followed link names and, when there is none or Keycloak no longer has that account, the
one under the canonical id. The email and names of an account whose username is not the canonical
id are never written, they are its owner's own, so a profile update of such a user sends nothing;
no second account is created for them. Deletes reach only the account under the canonical id.

What Keycloak does not do, or the adapter does not take on:

| Limit | What happens |
|---|---|
| A Keycloak user who signed up through the login page and has no `keycloak` IdP link, such as the realm's fixture users, has another username and is not taken over by email. | Keycloak refuses to create a second user with the same email (409); the adapter returns that error. A membership or status change for such a user is an error, logged and told as above. |
| The adapter treats any Keycloak user whose username equals a canonical user id as its own. | A realm must not let users choose their username where it can hold a canonical id: keep self-registration and brokered first login from setting usernames, and keep users from editing theirs (`editUsernameAllowed` off). The compose realm has registration off, no identity providers and `editUsernameAllowed` at Keycloak's default, off. |
| The admin client's grants reach further than the adapter needs: `manage-users` also creates realm groups, `manage` on the API client also changes that client's settings and reads its secret, and `create-client` registers any client through client registration. | Keep its secret like a realm admin's. |
| A push that fails leaves Keycloak behind the canonical model. | It is logged and told as above; the next [reconciliation](#reconciliation) pass re-pushes it. |
| An organization's alias cannot change in Keycloak. | The alias is the canonical tenant id, which does not change either, or the alias a tenant was linked to. |
| A canonical tenant's row is gone by the time `delete_tenant` runs. | The organization it reaches is the one whose alias is the canonical tenant id; a tenant linked to another alias keeps its organization. Nothing deletes a canonical tenant today. |
| An organization's domains must be valid domain names, each held by one organization in the realm. | A domain Keycloak refuses (400) is an error, and the organization keeps its name and domains. |
| An organization's name must be unique in the realm. | A second tenant with a name another organization holds is refused (409). |
| A client role's name is unique on the API client, for permissions and roles alike. | A permission and a role of the same name, or a name a realm admin gave another client role, are refused (`Refusal.NAME_HELD_BY_ANOTHER`). |

## Machine credentials

With `SYNC_PROVIDER=keycloak` the adapter declares the `machine_credentials` capability:
`/api/keys` creates, lists, reads, deactivates, activates and deletes a tenant's machine
credentials. Each is a `machine_credentials` row in Postgres (its tenant, its roles there, its
client and the `sub` of the client's tokens) and a client the adapter registers in the realm:

- **Created** through client registration (`POST /realms/{realm}/clients-registrations/default`)
  with the admin client's token and its `create-client` role: client id
  `identity-stack-machine-<credential id>`, confidential, service account on, no browser flow, no
  direct grant, full scope off, an audience mapper putting `KEYCLOAK_AUDIENCE` in its tokens, and
  the attributes `identity-stack.credential-id`, `identity-stack.tenant-id` and
  `identity-stack.organization` (the alias of the tenant's organization, which owns it). A tenant
  with no organization is refused. The secret is returned once and never stored; the registration
  access token Keycloak answers is stored on the row, for the delete, sealed with Fernet under
  `KEYCLOAK_REGISTRATION_TOKEN_KEYS` ([environment-variables.md](environment-variables.md)):
  Keycloak answers that token with the client's secret, so the database holds only ciphertext.
- **Deactivated and activated** by disabling and enabling the client's service account
  (`manage-users`): Keycloak 26.8.2 answers a disabled one's grant 401 `invalid_request`.
- **Deleted** with the registration access token (`DELETE .../clients-registrations/default/{client id}`);
  Keycloak deletes the service account with the client. A token Keycloak refuses for a client whose
  service account is gone counts as deleted. A delete Keycloak refuses otherwise deactivates the
  credential and keeps it, so it can be asked again.

The admin client reads and changes no client through the Admin REST API: creating a client there
needs `manage` on every client (`manage-clients`, or the fine-grained permission on all clients),
which would let it rewrite the SPA client. Client registration with `create-client` creates
clients only, and the registration access token manages just the one it created.

Keycloak 26.8 allows a protocol mapper type in an authenticated client registration only when both
the authenticated "Allowed Protocol Mapper Types" client registration policy and the realm attribute
`allowedProtocolMapperTypesForClientAdministrators` name it. The realm import and `infra/keycloak`
add `oidc-audience-mapper` to both, and to nothing else.

A machine's client-credentials token passes the token binding by its `azp`
(`identity-stack-machine-` prefix, `typ` `Bearer`, the API audience) and is authorized from its
credential alone (conventions §18): it belongs to the credential's tenant only and holds the
credential's roles there, and only while the credential is active and unexpired. A deactivated,
expired or deleted credential's tokens are refused at once, before they expire; Keycloak issues no
new ones to a deactivated or deleted client. A credential must carry at least one role, only roles
its creator holds in the tenant, and never `operator` (which would open every tenant's state to it
through `require_global_role`); it expires at most 365 days ahead. A machine's own token is refused
(403) on every `/api/keys` route, so no credential creates, renews or manages credentials, and on
the role assignment, role and permission definition and member routes, the FGA relation and schema
writes, a document's share and revoke, and the IdP-link writes, so no credential changes which
people hold which roles or relations, or belong to its tenant.

| Limit | What happens |
|---|---|
| The registration access token on each row holds the client's secret at rest (`machine_credentials.registration_token`): Keycloak's client registration endpoint returns the client's secret and changes the client (its flows, redirect URIs, secret). | The token is sealed with Fernet under `KEYCLOAK_REGISTRATION_TOKEN_KEYS` before it is written and opened only to delete the client; neither the keys nor the tokens are ever logged. Keep the database like a secret store: a token of a machine client whose `sub` is not the credential's grants nothing. |
| `allowedProtocolMapperTypesForClientAdministrators` lets the admin client add an `oidc-audience-mapper` to the API client too, which Keycloak refuses without it (probed, `keycloak/fixtures/README.md`). | It is the one type named, and the API client has no flow on, so no token of it reaches the SPA's users. |
| Keycloak logs that the `default` client registration provider is deprecated (`KC-SERVICES0112`) on each registration. | It works on 26.8.2. A Keycloak that removes it fails the create with a provider error; the alternative is an Admin API path that does not grant `manage` on every client. |
| Keycloak has no expiry for a client. | The backend refuses a credential's tokens after its expiry; the client stays until the credential is deleted. |

### Rotating `KEYCLOAK_REGISTRATION_TOKEN_KEYS`

`KEYCLOAK_REGISTRATION_TOKEN_KEYS` is a comma-separated list of Fernet keys. The first seals and
every listed key opens, so a new key goes first and the old ones stay listed while any row is
still sealed under them. A safe rotation needs both a clean reseal run and a rollout that puts
**every writer** of machine credentials on the new first key.

The reseal script, `python -m scripts.reseal_keycloak_registration_tokens` (run from `backend/`
with `DATABASE_URL` and `KEYCLOAK_REGISTRATION_TOKEN_KEYS` set), reads every non-empty
`machine_credentials.registration_token` for the `keycloak` provider and:

1. **First pass (reseal):** for each token that does *not* open under the first key, it tries to
   open it under the configured key set and, when it can, reseals it under the first key using a
   compare-and-set on the stored ciphertext (`WHERE id = ? AND registration_token = ?`). Tokens
   that open under no configured key are counted as unreadable and left untouched, so delete
   keeps `UNREADABLE_REGISTRATION_TOKEN` semantics.
2. **Commit:** all reseal updates are made in *one* database transaction. The script calls
   `commit()` once after the first pass. Writes to the same rows from the backend may block on
   row locks until that commit; reads continue under MVCC snapshots.
3. **Second pass (classification):** it reads the tokens again and classifies them as primary
   (open under the first key), non-primary (still open only under a later key) or unreadable. A
   token that changed between the snapshot and the reseal update and remained non-primary is
   reported as a compare-and-set conflict.

The script prints a summary and exits with:

- **1** when `KEYCLOAK_REGISTRATION_TOKEN_KEYS` is unset or invalid, without touching any row.
- **0** only when `remaining_non_primary`, `remaining_unreadable` and `cas_conflicts` are all
  zero.
- **2** otherwise (`INCOMPLETE: safe completion not established`).

A clean scan (exit 0) shows that, at the end of the run, no stored registration token opens only
under a non-primary key and none is unreadable under the configured key set. It does **not** prove
that all running backend instances are using the new first key or that no new old-key tokens can
appear afterwards.

A safe rolling rotation therefore runs as:

1. **Generate a new Fernet key B** out of band and keep it secret. Never log or commit the key.
2. **Set `KEYCLOAK_REGISTRATION_TOKEN_KEYS` to `B,A`** (or `B,A,...`) on every backend instance,
   with B first and all old keys still listed. Roll this configuration out to **all writers of
   machine credentials** and verify that each instance has restarted on the new value.
3. **Run the reseal script** once against the production database, in the same environment
   (`KEYCLOAK_REGISTRATION_TOKEN_KEYS=B,A`). Inspect its summary:
   - When it exits 2 or reports any `remaining_non_primary`, `remaining_unreadable` or
     `cas_conflicts`, keep A listed, resolve the misconfiguration or concurrent writer and repeat
     after the rollout is fixed.
   - When it exits 0 and reports all three as zero, the database holds no tokens that open only
     under non-primary keys under the current key set.
4. **Only after both a clean reseal run and a confirmed rollout** of `KEYCLOAK_REGISTRATION_TOKEN_KEYS=B,...`
   to all writers may old keys be removed from `KEYCLOAK_REGISTRATION_TOKEN_KEYS` (for example,
   to `B` alone). Redeploy that configuration to every backend instance.

The tool never reveals registration tokens or keys: it works only with ciphertext in the
database, log messages name counts and configuration variables, and the keys and tokens are
never printed or logged.

## Event pull

With `SYNC_PROVIDER=keycloak` and `KEYCLOAK_PULL_EVENTS` on (the default), what an administrator
changes in Keycloak comes back to the canonical model. Every `KEYCLOAK_PULL_INTERVAL_SECONDS`
(10) the backend reads the realm's admin events (`GET /admin/realms/{realm}/admin-events`, with
representations) and user events (`GET /admin/realms/{realm}/events`) through the
[admin adapter's](#admin-adapter) client, which holds `view-events`, and applies them, oldest first
across both stores, through `InboundSyncService.apply_keycloak_change`. The code is
`app/services/keycloak_pull.py` (the poll), `app/services/keycloak_events.py` (what an event
changes) and `app/dependencies/keycloak_pull.py` (the settings and the loop, a background task of
the application's lifespan). The compose realm keeps both stores 7 days.

| Keycloak change | Canonical change |
|---|---|
| Organization updated (admin event `UPDATE ORGANIZATION`) | The tenant linked to its alias (`external_org_id`, `keycloak` provider) takes its name, domains and status (`enabled` → `active`, disabled → `suspended`). |
| Organization deleted | That tenant is suspended, not deleted: its roles and records stay. |
| User updated (`UPDATE USER`; user events `UPDATE_PROFILE`, `UPDATE_EMAIL`, `VERIFY_EMAIL`) | The canonical user takes the email, given and family names the event carries; after a user's own profile change, the account's email only once Keycloak has verified it. A canonical user whose email the pull changed is marked `email_from_organization`: Keycloak chose that email, so no first login of any provider links to the user by it (conventions §28). `enabled` is taken only when the event records a change of it (`details.updated_enabled`): disabled → `inactive`, enabled → `active` for an `inactive` user (a `provisioned` one stays). |
| User deleted (`DELETE USER`; `DELETE_ACCOUNT`), or disabled by a permanent lockout | The canonical user is deactivated, as for Descope's `user.deleted`. |
| Organization member added | The user holds the default role `member` in the tenant, unless they already hold a role there: a canonical membership is a role in the tenant. |
| Organization member removed | Every role of the user in the tenant is removed. |
| User put in or taken out of an organization group | The role the group stands for (its `identity-stack.role-id`, else its name: `<name>` a global role, `<name>@<tenant id>` that tenant's own) is assigned or unassigned. |
| API client role created, updated or deleted | A client role carrying `identity-stack.permission-id`, or with no canonical id and a `<domain>:<action>` name, is a permission; any other is a role, global or, named `<name>@<tenant id>`, that tenant's own. It is created, renamed or given its description, or deleted. |
| Composites added to or removed from a role's client role | The role's permissions gain or lose the permissions among them. |

A user is the canonical user their `keycloak` IdP link names (the account id), else, for an account
the adapter created, the canonical user whose id is its username and who is linked to no other
account. The username is read that way only for an administrator's change (an admin event), never for
a user's own event: a username is not proof of who an account belongs to. Creating a user or an organization in Keycloak changes nothing canonical: a user is
provisioned at first sign-in, and a tenant is made on the canonical side. Everything else Keycloak
records (logins, clients, realm roles, credentials, groups themselves) is passed.

**Skipped.** An admin event whose author (`authDetails.userId`) is the admin client's service account
is the backend's own push coming back and changes nothing; so is a user event of the admin client
(`clientId`). The service account cannot read its own client's Keycloak id, and acts for that client
alone, so it stands for the client. An event naming an entity the canonical model does not map (an
organization no tenant is linked to, an account no canonical user is) is logged, recorded as a
`skipped` sync event and passed.

**Order and the cursor.** The cursor of each store, the ids of the events it has applied in the last
5 seconds before it, and the aliases and client role names of the realm (an event deleting one names
only its Keycloak id; kept until that delete is read) are one row of `keycloak_pull_state`, keyed by the issuer. A batch of at most
`KEYCLOAK_PULL_PAGE_SIZE` (100) events of each store is applied in one transaction, which commits the
canonical changes, their `sync_events` and the advanced cursor together: a restart, or a crash
mid-batch, resumes from the last commit and applies no event twice. The poll reads from 5 seconds
before the cursor and passes the events it has already applied by id, so two events in the same
millisecond, or one committed after a later one, are not lost. The first poll of a realm starts at the
newest events: what happened before the pull was enabled is [reconciliation's](#reconciliation).

**Several workers.** Each worker runs the loop; a batch first takes a Postgres advisory lock
(`pg_try_advisory_xact_lock`), and a worker that cannot take it leaves the poll to the one that holds
it.

**Failures.** A change the canonical model refuses (a name or email another row holds, no `member`
role), or that the database refuses outright, is rolled back alone (a savepoint), recorded as a
`failed` sync event and passed. When Keycloak cannot be asked while an event is read (no answer, no
service-account token, a 5xx, 401, 408 or 429), the events before it commit with their cursor and the
poll stops; the next one reads that event again. Any other refusal (a 403 is a grant the service
account lacks), or an answer that cannot be read, is recorded as `failed` and passed. When the event
stores themselves cannot be read, the poll stops and applies nothing. A client role renamed keeps its
kind (a permission stays a permission), and a rename that would move a tenant's role to another tenant
is skipped.

**Revocation.** Authorization reads the canonical model on every request (conventions §18), so a
user disabled, deleted or removed from an organization in Keycloak is refused (403) on the first
request after the poll that applies it: up to `KEYCLOAK_PULL_INTERVAL_SECONDS` plus the poll. Their
Keycloak tokens stay valid until they expire; the backend refuses them.

## Reconciliation

`POST /api/internal/reconciliation/run` (guarded by `DESCOPE_FLOW_SYNC_SECRET`, as for Descope) runs
one pass per configured provider: the provider the canonical model is pushed to (`SYNC_PROVIDER`),
when it has a reader. Descope and Keycloak have one; Ory and Zitadel are refused with 409 until theirs
is added as one more entry of `RECONCILIATION_PASSES` (`app/dependencies/identity.py`). Each pass runs
in its own transactions and is reported under `providers.<name>` of the answer, and its last outcome
under `reconciliation.<name>` of `GET /api/sync/status`, which also gives `pending_pushes`, the number of
canonical changes Keycloak is not yet confirmed to hold. The code is
`app/services/keycloak_reconciliation.py`, wired in `app/dependencies/keycloak_reconciliation.py`.

**Pending pushes.** A database trigger on `tenants`, `users`, `permissions`, `roles`,
`role_permissions` and `user_tenant_roles` (migration `009_pending_pushes`) marks the entity a write
changes in `pending_pushes`, in the transaction of the write: `tenant`, `user`, `permission`, `role`
(its permissions included) or `membership` (every role of a user in a tenant). An update that changes
nothing a push carries (a link the adapter writes, `updated_at`) marks nothing. A canonical change is
**not yet delivered** while its entity is marked: from the commit that makes it, through a push in
flight and a push that failed (`provider_sync: failed`), until a reconciliation pass has pushed the
entity's current state and removed the mark. Each mark has a `seq` that every later change raises, and
a pass removes a mark only up to the `seq` it read before it read the entity, so a change made during
the push keeps its mark. A successful push from a request does not remove the mark: the next pass
confirms it. The migration marks every entity that existed before it, so an entity that is not marked
has been delivered at least once. The writes of the [event pull](#event-pull) and of a pass itself are
Keycloak's state (`identity_stack.sync_origin` is `provider` for their transaction): they mark nothing,
and they raise the `seq` of an entity already marked.

**A pass**, with the event poller stopped or running:

One pass runs at a time: a pass holds a Postgres session lock on a connection of its own from start
to end (its second half commits after each push, which a transaction lock would not survive), and a
pass started while another holds it answers 409.

1. **Keycloak into Postgres.** In one transaction holding the reconciliation lock and the pull's lock,
   the pass reads the marks, then the realm: the organizations, the members and role groups (with their
   members) of those a tenant is linked to, the users, and the API client's roles with their composites.
   Every difference is applied as the event that would have made it, through
   `InboundSyncService.apply_keycloak_change` (the [event pull](#event-pull)'s table), each in a savepoint:
   - A linked organization's name, domains and `enabled` are the tenant's; one that is gone suspends it.
   - A linked account's names, `enabled`, and its email once Keycloak has verified it, are the user's;
     a linked account that is gone deactivates the user. An account stands for a canonical user only
     through its `keycloak` IdP link, never through a username that is a canonical user id: a realm
     that lets a person choose a username would let them name someone else. The accounts a pass creates
     are linked when it creates them; an account already under a canonical id is not.
   - Every API client role is created or updated as a permission or role; a canonical permission or
     role that no client role stands for (by its canonical id, or by name for an unstamped one) was
     deleted in Keycloak and is deleted. A role's composites are its permissions.
   - A member's role groups are their roles in the tenant. A member in no role group holds the default
     role `member`, as the pull reads a member added. A user Keycloak no longer has as a member holds
     no role there. So a role group deleted in the console, for which Keycloak records no member
     removal and the pull takes in nothing, unassigns its role at the next pass.

   A list is read a page at a time, so an organization, an account or a membership missing from it
   is asked for again by itself before it is taken as deleted.

   A change whose subject is marked is rolled back: Keycloak's older state does not overwrite a
   canonical change not yet delivered. Before committing, the pass reads the marks of every entity it
   changed again; a canonical change committed during the read marks one of them, and then the pass
   commits nothing of this half, still runs the second, and answers 409, to be run again. A poll waits for this half (its lock is taken
   blocking here, and the poll only tries it), so a batch and the pass never interleave.
2. **Postgres into Keycloak.** Each marked entity, in the order tenant, permission, role, user,
   membership, is pushed from its current canonical state through `KeycloakSyncAdapter`, whose pushes
   are idempotent: a tenant, a permission, a role with its permissions, a user's profile then status, or
   every role group of a membership (a membership with no role left is ended, the case of
   [identity-stack#612](https://github.com/jamescrowley321/identity-stack/issues/612)). An entity whose
   row is gone is deleted in Keycloak. When every push for it is delivered its mark is removed; when one
   fails, the mark stays for the next pass and a `failed` sync event (`detail.source` `reconciliation`)
   records the operation and the error. A tenant linked to another provider's organization, and its
   memberships, have nothing to push. A deleted tenant's organization is the one its link named (the
   mark keeps it).

When the realm cannot be read, the pass applies and pushes nothing and answers 502.

## Upgrading from the token-authorized model

The model before [ADR-0011](adr/0011-keycloak-canonical-authorization.md) read a Keycloak
principal's grants from its token, and its first sign-in (with `keycloak` in
`JIT_ENABLED_PROVIDERS`) linked the Keycloak identity to any existing canonical user with the
same verified email. Under the canonical model such a link gives its Keycloak user every
canonical role of the linked user. Before listing `keycloak` in `CANONICAL_RBAC_PROVIDERS` on
a database that model wrote to, delete the links its sign-ins made, then let those users sign
in again: each is provisioned under the organization rules, or refused.

```sql
DELETE FROM idp_links
WHERE provider_id = (SELECT id FROM providers WHERE name = 'keycloak')
  AND metadata->>'linked_by' = 'sign_in';
```

Run it with `psql "$DATABASE_URL"` (the URL without the `+asyncpg` driver suffix), with the
backend stopped or before it starts with the new setting. Links an operator or a seed wrote
(`linked_by` `backend`, or no origin) are kept: they name the user they were meant for. A
canonical user the old path created for a Keycloak identity keeps its rows and any role an
operator gave it; deactivate it if it should not sign in again.

## Limits found

| Limit | Where it shows | Follow-up |
|---|---|---|
| A console change reaches the canonical model at the next poll, not at once: until then the backend authorizes from what it held. A disabled account's unexpired token is served for up to one `KEYCLOAK_PULL_INTERVAL_SECONDS` (10 s by default). | Cases 7 and 8 | None: the interval is the bound. An event-listener extension posting signed webhooks would remove the wait, at the cost of one more artifact to build and pin |
| On a database with no canonical roles, the first reconciliation pass creates the roles from the API client's client roles but reads every role group in the same pass as no role, so each member gets the default `member`; the second pass assigns the groups' roles. | Suite set-up | Open |
| A user linked to an account under another username (a sign-in's link, or one `make keycloak-seed` wrote) gets its status, memberships and role groups pushed to that account, never its email or names, which are the account's own. A canonical delete reaches only the account under the canonical id. | [Which Keycloak account](#which-keycloak-account) | None for the profile: by design. Deletes: no route deletes a canonical user today |
| Signing out ends the SPA's Keycloak session only. The user's other sessions (another browser, another client) stay. | Case 5 | None |
| The organization a user picks at sign-in is in the token's `organization` claim and authorizes nothing after first sign-in: the SPA's tenant selector starts on that organization's tenant (each membership in `GET /api/identity` carries its `external_org_id`), and the tenant is the one `X-Tenant-ID` names. | Cases 1 and 3, `backend/tests/e2e_keycloak/test_sign_in_tenant.py` | None: by design ([ADR-0011](adr/0011-keycloak-canonical-authorization.md)) |
| The routes that still send the tenant or the subject to Descope (members, roles and permissions, access keys, FGA and documents, tenant settings, profile writes) answer a Keycloak principal 403. The tenant list, the current tenant and the tenant resources serve it from its canonical memberships ([identity-stack#673](https://github.com/jamescrowley321/identity-stack/issues/673)). | `backend/tests/e2e/not_applicable.py` (`DESCOPE_ONLY_ROUTE`, `TENANT_SETTINGS`) | Open for the Descope-backed routes: [identity-stack#672](https://github.com/jamescrowley321/identity-stack/issues/672) |
| The realm registers one SPA origin, `http://localhost:3000`, so the Keycloak end-to-end suite runs in a job of its own, apart from the Descope one that serves its SPA there. | CI | None |
| Keycloak sets its session cookies `Secure` on the plain-HTTP origin. Chromium keeps them for a `*.localhost` host; other clients may not, and the login form then answers 400. | Every browser login | None: local and CI only |
