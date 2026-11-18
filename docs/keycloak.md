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

With `SYNC_PROVIDER=keycloak` the canonical services push tenants, users and memberships to the
realm through `KeycloakSyncAdapter` (`backend/app/services/adapters/keycloak.py`) instead of
Descope. It writes through the Admin REST API with one service account,
`KEYCLOAK_ADMIN_CLIENT_ID`/`KEYCLOAK_ADMIN_CLIENT_SECRET` (`identity-stack-admin` in the compose
realm: `realm-management` `manage-users` and `manage-organizations`, client scope `kc-admin-api`),
on one HTTP client with a 5-second timeout. Startup takes a token, makes one Admin API read with
it and reads the service account's own `realm-management` roles (composites expanded); a missing
setting, a credential Keycloak refuses, or a service account without `manage-users` and
`manage-organizations` (such as the read-only `identity-stack-directory`) stops the backend before
it serves a request ([environment-variables.md](environment-variables.md)). Map the two roles to the
service account directly: startup reads its direct `realm-management` mappings, so roles it reaches
only through a group or a composite realm role are not seen and stop startup too (reading them needs
`view-clients`, which the admin client does not hold). No route changes: the services that synced to
Descope sync to Keycloak instead. Reconciliation (`POST /api/internal/reconciliation/run`) compares
the canonical model with Descope, so with `SYNC_PROVIDER=keycloak` it is refused (409).

| Canonical change | What the adapter does in Keycloak |
|---|---|
| A tenant is created (`sync_tenant`) | Creates the organization whose alias is the canonical tenant id, named as the tenant, with its `self_provisioning_domains` as domains. Pushed again, it updates the name of the organization it finds by that alias and, when `self_provisioning_domains` is given, sets its domains to exactly that list (`[]` clears them). Keycloak resets the fields an update leaves out, so the adapter sends the organization as it found it with only those fields replaced: a realm admin's `enabled`, description and redirect URL stay. It never sends the organization's attributes, the realm admin's `type:<type client>` grant markers, and Keycloak keeps them (ending a membership takes only that member out of them, below). Domains that are not a list of names are an error, and nothing is sent. |
| A tenant is deleted (`delete_tenant`) | Deletes that organization. One that is not there counts as deleted. |
| A user is created or updated (`sync_user`) | Creates or updates the user's [Keycloak account](#which-keycloak-account): with none, the user whose username is the canonical user id, with the canonical email. A user whose sign-in link names an account gets no second one, and nothing is sent to it. A new user is enabled unless its status is `inactive`. An update does not send `enabled`, so it keeps a disable or enable a realm admin made in Keycloak, and an update that leaves the email as it is sends nothing (names are not pushed). A changed email is set unverified (`emailVerified: false`): the realm links identities across providers by verified email, and the new address has not been verified. |
| A user is deactivated or activated | Disables (`inactive`) or enables (`active`, `provisioned`) the user's [Keycloak account](#which-keycloak-account). This overwrites a disable or enable a realm admin made in Keycloak. With no account, it is an error and no user is created, so a deactivation never leaves a disabled account beside the user's real one. |
| A user is deleted (`delete_user`) | Deletes that user. One that is not there counts as deleted. |
| A role is assigned in a tenant (`sync_role_assignment`) | Adds the user's [Keycloak account](#which-keycloak-account) to the tenant's organization: holding a role in a tenant is being its member. Already a member counts as added. `RoleService` assigns a role only to a user who already holds one in the tenant, and refuses anyone else (403, nothing written): a tenant admin cannot make a user of another tenant a member of their organization. A user's first membership comes from signing in (just-in-time provisioning puts them in the organization their token names), as [identity-stack#592](https://github.com/jamescrowley321/identity-stack/issues/592) has it for every provider. |
| A user is removed from a tenant (`remove_user_from_tenant`) | [Ends](#ending-a-membership) the membership of the user's [Keycloak account](#which-keycloak-account) in that tenant's organization, before the profile update, so a refused update still ends it. Not a member, or no organization, counts as removed. No account is an error, and no user is created: leaving a tenant never provisions an account. Then pushes only a changed email, and only to an account under the canonical id: leaving a tenant does not send `enabled`, so it does not undo a realm admin's disable. A role given to the user in the tenant while the removal was on its way is found after it, and its membership is added back. |
| A user's last role in a tenant is unassigned (`delete_role_assignment`) | [Ends](#ending-a-membership) the membership. `RoleService` marks the unassignment that leaves no role in the tenant, counting the roles left after its commit, so two concurrent unassigns of a user's last two roles cannot each see the other's role. The revoked role's cached permissions are invalidated before that count: if the count fails, the request fails after its commit and the membership stays (identity-stack#612). Not a member, or no organization, counts as removed; no account is an error. |
| A role is deleted (`delete_role`) | For each user the role was the last role of in a tenant, [ends](#ending-a-membership) their membership, as when that last role is unassigned. `RoleService` locks the role's row and reads who holds it before the delete (it removes their assignments with it), so a role assignment committing in between waits and then fails instead of being dropped unseen. After its commit it reads, in one query, who still holds a role in each tenant; a user with another role in the tenant stays a member. The pushes stop at the first Keycloak gives no answer to (each would wait out the same timeout), and the users not pushed are logged. The delete stands. |
| A role or permission is defined or changed, a permission is deleted; one role of several is unassigned | Nothing. Roles and permissions stay canonical, as with Ory; a user keeps the membership while another role in the tenant remains. |

Every failure comes back to the service as an error, with the HTTP status, and is logged; the
canonical change stands, as with Descope. Nothing raises. A failure of the service's own database
during a push (taking the organization lock, or not within its wait limit, reading the user's IdP
link) is such an error too, with no status: Keycloak was not asked, so `delete_role` goes on to the next user. A push that takes access away (ending a
membership, a deactivation) and fails is logged as an `ERROR` beginning `IdP revocation failed`,
and the service's answer carries `"provider_sync": "failed"`: Keycloak still grants what the
canonical change took away until the push is made again (identity-stack#612). `DELETE
/api/roles/{name}` returns that answer; `POST /api/roles/remove` builds its own and does not show
it, so alert on the log.

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

Memberships (added and ended), status changes and profile pushes reach the account the followed
link names and, when there is none or Keycloak no longer has that account, the one under the
canonical id. A linked account's email is never written, it is its owner's own, so a profile update
of a linked user sends nothing; no second account is created for them. Deletes reach only the
account under the canonical id.

### Ending a membership

Before ending a membership the adapter reads the organization and takes the member's account id out
of every `type:<type client>` grant marker, dropping a marker left with no member; other attributes
stay. Keycloak takes an organization's attributes only whole, so the organization is sent back as
read. The read, that write and the removal run under a Postgres advisory lock on the organization's
alias, as does a tenant update (also sent back as read), so two such writes from any backend worker
never interleave and the later never undoes the earlier. The lock is taken on the connection the
request's database session already holds, in a savepoint of its transaction, and released by rolling
that savepoint back (the push runs after the service committed, so the savepoint holds only the lock
and the rows the request loaded stay loaded), so a request never waits for a second pooled
connection. A push waits for the lock at most `KEYCLOAK_ORGANIZATION_LOCK_TIMEOUT_SECONDS` (default
20, shorter than the pool's 30 s checkout timeout; it does not bound how long a holder keeps the lock,
so a push queued behind a holder slower than the limit gives up): requests queued behind a slow holder each hold their connection while they wait, and the limit
lets them go before they stall other routes waiting for the pool. It does so only while the
requests holding or waiting for one organization's lock are no more than the pool's connections: a
route queued behind more of them waits through two rounds of waiters (2 x 20 s) and its 30 s checkout
times out. A push that gives up is a failed
push, logged and told as below (a membership ending is an `IdP revocation failed`); making it again
ends the membership. A realm admin's edit in Keycloak takes no
such lock: one made between the adapter's read and its write is overwritten. A later membership therefore starts with no grant a realm admin gave the earlier one. Markers
go first: should the removal then fail, the member is left without the grant rather than with it.
When `RoleService` or `UserService` finds, after the removal, that the user was given a role in the tenant meanwhile
(an assignment racing the unassignment pushed its membership before the removal reached Keycloak),
it adds the membership back. The unit tests replay answers recorded
from Keycloak 26.8.2 ([`backend/tests/fixtures/keycloak_admin/`](../backend/tests/fixtures/keycloak_admin/README.md)).

What Keycloak does not do, or the adapter does not take on:

| Limit | What happens |
|---|---|
| A Keycloak user who signed up through the login page and has no `keycloak` IdP link, such as the realm's fixture users, has another username and is not taken over by email. | Keycloak refuses to create a second user with the same email (409); the adapter returns that error. A membership or status change for such a user is an error, logged and told as above: the adapter cannot find their account, so a membership they hold stays until a realm admin ends it. |
| The adapter treats any Keycloak user whose username equals a canonical user id as its own. | A realm must not let users choose their username where it can hold a canonical id: keep self-registration and brokered first login from setting usernames, and keep users from editing theirs (`editUsernameAllowed` off, and no user-profile permission letting the user edit `username`). The compose realm has registration off, no identity providers and `editUsernameAllowed` at Keycloak's default, off. |
| The admin client's `manage-organizations` can write organization attributes, the `type:<type client>` grant markers the directory lookup trusts. | The adapter writes them only to take an ending member out, but its secret grants as much as a realm admin's over organizations: keep it like one. |
| The organization lock is a Postgres advisory lock. | `SYNC_PROVIDER=keycloak` needs `DATABASE_URL` on Postgres; on another database every push that reads and writes back an organization fails, and is told as above. |
| A push that fails leaves Keycloak behind the canonical model; nothing retries it. | A failed membership removal keeps the user in the organization, and a failed deactivation keeps the account enabled; both are told as above. Retrying or reconciling them is [identity-stack#612](https://github.com/jamescrowley321/identity-stack/issues/612). |
| An organization's alias cannot change in Keycloak. | The alias is the canonical tenant id, which does not change either. |
| An organization's domains must be valid domain names, each held by one organization in the realm. Keycloak's domains route a login to an organization's identity provider; they do not add users to it the way Descope's self-provisioning domains do. | A domain Keycloak refuses (400) is an error, and the organization keeps its name and domains. |
| An organization's name must be unique in the realm. | A second tenant with a name another organization holds is refused (409). |
| A role assignment is not mirrored to client roles. Client roles are realm-wide and say nothing of an organization (decision 10); the realm admin grants them and the `type:` markers. | The membership is pushed; the grant is not. Ending a membership takes the user out of the organization's `type:` markers, but leaves the user's client roles: the realm admin owns them and removes them. |
| A membership can be pushed only once the tenant and the user are both in Keycloak. | Assigning a role in a tenant that was never pushed returns an error. |

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
