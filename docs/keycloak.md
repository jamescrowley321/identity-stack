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
| `backend/tests/e2e_keycloak/` | The shipped SPA and backend against the realm `infra/keycloak/` builds in `keycloak-test`, configured from the root's outputs, driven from Chromium. No credential. | `E2E Tests (Keycloak)`, every pull request |
| `backend/tests/e2e/` with `E2E_PROVIDER=keycloak` | The shared UI and API suite ([local-development.md](local-development.md#end-to-end-suites)) against the same stack: `alice` (member, `member-app`) and `dave` (tenant admin of `acme`, `tenant-admin-app`). The tests that do not apply to Keycloak are listed with the reason in `backend/tests/e2e/not_applicable.py` | `E2E Tests (Keycloak)`, every pull request |
| `backend/tests/integration/test_keycloak_*.py` against the import | Token validation, organizations and client roles, `GET /api/identity` and the organization lookup, in-process; every write of the [admin adapter](#admin-adapter) and its startup credential check | `Integration Tests` |
| The same tests against the Terraform-built realm (`make keycloak-terraform-integration`) | `infra/keycloak/` builds a realm the backend accepts the same way: generated user ids, passwords and directory and admin secrets, same results | `Integration Tests` |
| `make keycloak-terraform-check` | Every user, client, role, organization, member and marker of the import is in the Terraform root and the other way round, and both realms issue tokens of the same shape | `Keycloak Terraform Check`, every pull request |

The end-to-end suite asks for each of these, in order:

1. `bob`, a member of `acme` and `globex`, signs in from the SPA through `member-app` for `acme`.
2. `GET /api/identity` returns him, the token's tenant `acme` with the permissions it grants
   there (`documents:read`) and both organizations as memberships.
3. `GET /api/access-checks/documents-read` answers in `acme`;
   `GET /api/access-checks/settings-manage` is refused (403): a `member` token has no
   `settings:manage`.
4. He switches to `globex` in the tenant switch. Keycloak answers the new authorize request on
   the live session with redirects only, never a page of its own, so no login form appears.
   The new token names `globex`, keeps the session id, and the API answers in `globex`.
5. He signs out from the user menu. Keycloak no longer lists the session, the SPA holds no
   token, and Sign In shows Keycloak's login form.
6. `carol`, who holds no client role, gets a token with no `aud`. Every route in the backend's
   OpenAPI document that needs a token answers 401 with the one rejection body.
7. `bob` through `tenant-admin-app` is refused `settings:manage` and `GET /api/identity` (403)
   for `acme` and for `globex`, because he belongs to two organizations. `dave`, tenant admin
   of `acme` alone, is allowed there.
8. `alice`, outside `globex`, asks for it on her live session. Keycloak shows the password
   page and then issues a token with no `organization` claim: the API answers it 403. Her
   `acme` token with `X-Tenant-ID: globex` is still answered in `acme`.
9. `alice`, who holds `documents:read` and `documents:write`, is added to `globex` for the
   test: in both organizations `GET /api/identity` lists only `documents:read`. `erin`, whose
   one role is `documents:write`, is granted nothing.

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

cd backend
export DATABASE_URL=postgresql+asyncpg://identity_test:identity_test@localhost:15432/identity_test
python -m alembic upgrade head
KEYCLOAK_ISSUER_URL=<KEYCLOAK_ISSUER_URL above> python -m scripts.seed_keycloak
# then start the backend on :8000 with the backend lines (the KEYCLOAK_ADMIN_CLIENT_* pair is
# needed only with SYNC_PROVIDER=keycloak), DESCOPE_PROJECT_ID= DESCOPE_MANAGEMENT_KEY= and
# JIT_ENABLED_PROVIDERS=keycloak, plus RATE_LIMIT_DEFAULT=2000/minute RATE_LIMIT_AUTH=1000/minute:
# the suite's refused requests share one client address, and the 60/minute default would
# answer some of them 429
# and INTERNAL_IDENTITY_KEY, DESCOPE_WEBHOOK_SECRET and DESCOPE_FLOW_SYNC_SECRET set to throwaway
# values, exported the same in the shell that runs the suite

cd ../frontend
# the frontend lines, with the suite's scopes: bob starts in acme, and a tenant switch asks
# for the organization picked
export VITE_OIDC_AUTHORITY=... VITE_OIDC_CLIENT_ID=... VITE_OIDC_REDIRECT_URI=...
VITE_OIDC_SCOPE="openid profile email organization:acme" \
  VITE_OIDC_TENANT_SCOPE_TEMPLATE="openid profile email organization:{tenant}" npm run build
npx vite preview --port 3000 --strictPort

make test-e2e-keycloak   # from the repository root; the shared suite, then backend/tests/e2e_keycloak/;
                         # reads the realm, user ids and passwords from the outputs
```

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
`realm-management` roles `manage-users`, `manage-organizations`, `query-clients` and
`view-events`, and the fine-grained admin permissions `view`, `manage` and `map-roles-composite` on
the API client (`KEYCLOAK_AUDIENCE`) alone; [`keycloak/fixtures/README.md`](../keycloak/fixtures/README.md#the-admin-service-account)
records what each grant allows and refuses. Startup takes a token, makes one Admin API read with it,
reads the service account's own `realm-management` roles (composites expanded) and looks the API
client up, and stops the backend before it serves a request when a setting is missing, Keycloak
refuses the credential, a role is missing, or the API client cannot be found or its roles managed
([environment-variables.md](environment-variables.md)). Map the roles to the service account
directly: roles it reaches only through a group or a composite realm role are not seen. No route
changes: the services that synced to Descope sync to Keycloak instead. Reconciliation
(`POST /api/internal/reconciliation/run`) compares the canonical model with Descope, so with
`SYNC_PROVIDER=keycloak` it is refused (409).

| Canonical | Keycloak | What the adapter pushes |
|---|---|---|
| Tenant | Organization | `sync_tenant` creates the tenant's organization, or updates it: name, domains exactly the tenant's `self_provisioning_domains` when given (`[]` clears them), and `enabled` from the tenant's `status` when given (`active` enabled, `suspended` disabled). The tenant's organization is the one the tenant is linked to (`tenants.external_org_id` of the `keycloak` provider, as `make keycloak-seed` links the realm's fixture organizations), else the one whose alias is the canonical tenant id. One it creates is then linked to the tenant, so a first sign-in through it finds the tenant; a tenant linked to another provider's organization is refused. Keycloak resets the fields an update leaves out, so the organization is sent back as found with only those fields replaced, and not at all when it already matches. `delete_tenant` deletes the organization; one not there counts as deleted. |
| User | User | `sync_user` creates or updates the user's [Keycloak account](#which-keycloak-account): with none, the user whose username is the canonical user id, with the canonical email and the given and family names (`firstName`, `lastName`). A user created in a tenant (`UserService.create_user`) is then linked to it (`idp_links`, `keycloak`, `{"linked_by": "backend"}`), so its first sign-in finds it. A new user is enabled unless its status is `inactive`; a profile update does not send `enabled`, so a realm admin's enable or disable stands, while activate and deactivate (`STATUS_CHANGED`) push it. A changed email is set unverified (`emailVerified: false`): the realm links identities across providers by verified email. A status change for a user Keycloak lacks is an error and creates no account. `delete_user` deletes the account under the canonical id; one not there counts as deleted. |
| Membership | Organization member | A role assignment makes the user a member of the tenant's organization (below). The membership ends when the user's last role in the tenant is unassigned (`LAST_ROLE_IN_TENANT`), when a role deleted was their last there, or when the user is removed from the tenant (`remove_user_from_tenant`, before any profile update, so a refused update still ends it; it sends no `enabled`). Ending it takes the user out of every group of the organization too. A user Keycloak lacks is an error, and no account is created. |
| Permission | Client role `<domain>:<action>` of the API client | `sync_permission` creates or updates it (name, description), with the permission's id in the attribute `identity-stack.permission-id`; a rename (`previous_name`) renames it. `delete_permission` deletes it, and Keycloak drops it from every role's composites. |
| Role | Composite client role of the API client | `sync_role` creates or updates it, with the role's id in `identity-stack.role-id`, and, when the role's permissions are given (`permission_names`), sets its composites to exactly their client roles; a permission not pushed yet is an error. A global role's client role is named as the role, a tenant's own role `<name>@<tenant id>`, so two tenants' roles never share a name. A rename renames the role's groups in every organization (a tenant's own role: in its tenant's), then the client role. `delete_role` deletes the role's groups, then its client role. |
| Role assignment | Membership of the organization's group for the role | `sync_role_assignment` makes the user a member of the tenant's organization and of the organization's group named as the role's client role (created, with `identity-stack.role-id`, when the organization has none). The group maps no role, so the membership records the assignment and grants nothing a token carries. Unassigning takes the user out of the group; the last role in the tenant ends the membership. The tenant, the user and the role must have been pushed already. |

A client role or group is found by the canonical id it carries, else by its name when it carries
none: the realm import's permissions, roles and groups carry none, and the first push adopts them
(stamps the id). One of that name that carries another canonical id is refused. Nothing the adapter
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
meanwhile, it adds the membership back.

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
still holds what the canonical change took away until the push is made again (reconciliation,
[identity-stack#661](https://github.com/jamescrowley321/identity-stack/issues/661)). Authorization
never reads it: a removed membership is refused on the next request from the canonical model.
`DELETE /api/roles/{name}` returns that answer; `POST /api/roles/remove` builds its own and does not
show it, so alert on the log. The unit tests replay answers recorded from Keycloak 26.8.2
([`backend/tests/fixtures/keycloak_admin/`](../backend/tests/fixtures/keycloak_admin/README.md)).

### Which Keycloak account

The adapter creates users under the canonical user id (their username). A user who signed in
through Keycloak first has their own account, linked to the canonical user by a `keycloak` IdP
link whose subject is the account's id. The adapter follows only a link a sign-in made:
just-in-time provisioning records it on the link (`metadata` `{"linked_by": "sign_in"}`), from the
validated token's subject, and `POST /api/users/{id}/idp-links` refuses a link that sets that key
(422). A link a tenant admin made names whatever account the admin typed in, so the adapter never
writes to it; nor to a client's service account (`service-account-*`, or one with a
`serviceAccountClientId`), whatever link names it. Links made before this rule carry no origin and
are not followed either.

Memberships (added and ended), group memberships, status changes and profile pushes reach the
account the followed link names and, when there is none or Keycloak no longer has that account, the
one under the canonical id. A linked account's email and names are never written, they are its
owner's own, so a profile update of a linked user sends nothing; no second account is created for
them. Deletes reach only the account under the canonical id.

What Keycloak does not do, or the adapter does not take on:

| Limit | What happens |
|---|---|
| A Keycloak user who signed up through the login page and has no `keycloak` IdP link, such as the realm's fixture users, has another username and is not taken over by email. | Keycloak refuses to create a second user with the same email (409); the adapter returns that error. A membership or status change for such a user is an error, logged and told as above. |
| The adapter treats any Keycloak user whose username equals a canonical user id as its own. | A realm must not let users choose their username where it can hold a canonical id: keep self-registration and brokered first login from setting usernames, and keep users from editing theirs (`editUsernameAllowed` off). The compose realm has registration off, no identity providers and `editUsernameAllowed` at Keycloak's default, off. |
| The admin client's grants reach further than the adapter needs: `manage-users` also creates realm groups, and `manage` on the API client also changes that client's settings and reads its secret. | Keep its secret like a realm admin's. |
| A push that fails leaves Keycloak behind the canonical model. | It is logged and told as above; reconciliation ([identity-stack#661](https://github.com/jamescrowley321/identity-stack/issues/661)) re-pushes it. |
| An organization's alias cannot change in Keycloak. | The alias is the canonical tenant id, which does not change either, or the alias a tenant was linked to. |
| A canonical tenant's row is gone by the time `delete_tenant` runs. | The organization it reaches is the one whose alias is the canonical tenant id; a tenant linked to another alias keeps its organization. Nothing deletes a canonical tenant today. |
| An organization's domains must be valid domain names, each held by one organization in the realm. | A domain Keycloak refuses (400) is an error, and the organization keeps its name and domains. |
| An organization's name must be unique in the realm. | A second tenant with a name another organization holds is refused (409). |
| A client role's name is unique on the API client, for permissions and roles alike. | A permission and a role of the same name, or a name a realm admin gave another client role, are refused (`Refusal.NAME_HELD_BY_ANOTHER`). |

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
| An organization-bound type is refused to anyone in two organizations, in both (decision 10). `bob` cannot administer either organization. | Scope 7 | By design |
| A portable type carries only the read permissions listed for it. A write permission a user holds on `member` is never granted. | Scope 9 | By design |
| Only `settings:manage` of the `tenant-admin` permissions has a route that checks it. `members:invite` and `members:remove` are in the realm and in tokens and are gated nowhere yet. | Scope 7 | Open: the [admin adapter](#admin-adapter) pushes memberships from the canonical model, and no route checks these permissions |
| Keycloak does not refuse an authorize request for an organization the user is not in. It issues a token without the `organization` claim, so the backend refuses that token (403), not Keycloak. | Scope 8 | None: the backend refuses |
| A user removed from an organization keeps a portable token's permissions until it expires (300 s with realm defaults). Keycloak refuses the next refresh. An organization-bound token is checked against the directory on every request. | [`keycloak/fixtures/README.md`](../keycloak/fixtures/README.md#leaving-an-organization-while-holding-a-token-for-it) | Recorded (decision 13) |
| Signing out ends the SPA's Keycloak session only. The user's other sessions (another browser, another app client) stay. | Scope 5 | None |
| A silent token renewal can bring back a token for another organization than the one shown. | SPA | [identity-stack#602](https://github.com/jamescrowley321/identity-stack/issues/602) |
| Routes that send the tenant to Descope, parse it as a canonical tenant id or gate by role answer a Keycloak principal 403, `GET /api/tenants` included. | [architecture.md](architecture.md#keycloak-principals) | Open: the [admin adapter](#admin-adapter) changes no route |
| The realm registers one SPA origin, `http://localhost:3000`, so the Keycloak end-to-end suite runs in a job of its own, apart from the Descope one that serves its SPA there. | CI | None |
| Keycloak sets its session cookies `Secure` on the plain-HTTP origin. Chromium keeps them for a `*.localhost` host; other clients may not, and the login form then answers 400. | Every browser login | None: local and CI only |
