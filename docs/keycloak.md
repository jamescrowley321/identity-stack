# Keycloak: what is proved, and its limits

identity-stack works against a Keycloak realm with Organizations as tenants and client roles as
permissions ([ADR-0010](adr/0010-keycloak-token-model.md),
[architecture.md](architecture.md#keycloak-token-model)). This page records what the automated
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
| `backend/tests/e2e_keycloak/` | The shipped SPA and backend against the compose realm, driven from Chromium. No credential. | `E2E Tests (Keycloak)`, every pull request |
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
there. With the test stack up (`make test-up-keycloak`) and `keycloak-test.localhost` resolving
to loopback:

```bash
cd backend
export DATABASE_URL=postgresql+asyncpg://identity_test:identity_test@localhost:15432/identity_test
export KEYCLOAK_ISSUER_URL=http://keycloak-test.localhost:18180/realms/identity-stack
python -m alembic upgrade head
python -m scripts.seed_keycloak
# then start the backend on :8000 with the KEYCLOAK_* settings of local-development.md
# (issuer above), DESCOPE_PROJECT_ID= DESCOPE_MANAGEMENT_KEY= and JIT_ENABLED_PROVIDERS=keycloak,
# plus RATE_LIMIT_DEFAULT=2000/minute RATE_LIMIT_AUTH=1000/minute: the suite's refused requests
# share one client address, and the 60/minute default would answer some of them 429

cd ../frontend
VITE_OIDC_AUTHORITY=$KEYCLOAK_ISSUER_URL VITE_OIDC_CLIENT_ID=member-app \
  VITE_OIDC_SCOPE="openid profile email organization:acme" \
  VITE_OIDC_TENANT_SCOPE_TEMPLATE="openid profile email organization:{tenant}" npm run build
npx vite preview --port 3000 --strictPort

make test-e2e-keycloak   # from the repository root
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
it serves a request ([environment-variables.md](environment-variables.md)). No route changes: the services
that synced to Descope sync to Keycloak instead.

| Canonical change | What the adapter does in Keycloak |
|---|---|
| A tenant is created (`sync_tenant`) | Creates the organization whose alias is the canonical tenant id, named as the tenant, with its `self_provisioning_domains` as domains. Pushed again, it updates the name of the organization it finds by that alias and, when `self_provisioning_domains` is given, sets its domains to exactly that list (`[]` clears them). Keycloak resets the fields an update leaves out, so the adapter sends the organization as it found it with only those fields replaced: a realm admin's `enabled`, description and redirect URL stay. It never sends the organization's attributes, the realm admin's `type:<type client>` grant markers, and Keycloak keeps them. Domains that are not a list of names are an error, and nothing is sent. |
| A tenant is deleted (`delete_tenant`) | Deletes that organization. One that is not there counts as deleted. |
| A user is created or updated (`sync_user`) | Creates or updates the user whose username is the canonical user id, with the canonical email. A changed email is set unverified (`emailVerified: false`): the realm links identities across providers by verified email, and the new address has not been verified. |
| A user is deactivated or activated | Disables (`inactive`) or enables (`active`, `provisioned`) that user. This overwrites a disable or enable a realm admin made in Keycloak. |
| A user is deleted (`delete_user`) | Deletes that user. One that is not there counts as deleted. |
| A role is assigned in a tenant (`sync_role_assignment`) | Adds the user to the tenant's organization: holding a role in a tenant is being its member. Already a member counts as added. |
| A user is removed from a tenant (`remove_user_from_tenant`) | Ends the user's membership of that tenant's organization, before the profile update, so a refused update still ends it. Not a member, or no organization, counts as removed. Then pushes only a changed email: leaving a tenant does not send `enabled`, so it does not undo a realm admin's disable. |
| A user's last role in a tenant is unassigned (`delete_role_assignment`) | Ends the user's membership of that tenant's organization. `RoleService` marks the unassignment that leaves no role in the tenant, counting the roles left after its commit, so two concurrent unassigns of a user's last two roles cannot each see the other's role. Not a member, or no organization or user in Keycloak, counts as removed. |
| A role or permission is defined, changed or deleted; one role of several is unassigned | Nothing. Roles and permissions stay canonical, as with Ory; a user keeps the membership while another role in the tenant remains. |

Every failure comes back to the service as an error, with the HTTP status, and is logged; the
canonical change stands, as with Descope. Nothing raises. The unit tests replay answers recorded
from Keycloak 26.8.2 ([`backend/tests/fixtures/keycloak_admin/`](../backend/tests/fixtures/keycloak_admin/README.md)).

What Keycloak does not do, or the adapter does not take on:

| Limit | What happens |
|---|---|
| The adapter manages only the users it created (username = canonical user id). A Keycloak user who signed up through the login page, such as the realm's fixture users, has another username and is not taken over by email. | Keycloak refuses to create a second user with the same email (409); the adapter returns that error. A membership change for a user the adapter did not create returns an error too. |
| The adapter treats any Keycloak user whose username equals a canonical user id as its own. | A realm must not let users choose their username where it can hold a canonical id: keep self-registration and brokered first login from setting usernames (the compose realm has registration off and no identity providers). |
| The admin client's `manage-organizations` can write organization attributes, the `type:<type client>` grant markers the directory lookup trusts. | The adapter never sends attributes, but its secret grants as much as a realm admin's over organizations: keep it like one. |
| A tenant admin can assign a role to a user who is not a member of the tenant (`POST /api/roles/assign` does not check the target's membership, [identity-stack#592](https://github.com/jamescrowley321/identity-stack/issues/592)). | With this adapter that adds the user to the tenant's organization. An organization-bound user who is a member of two organizations is refused in both, so another tenant's admin can lock such a user out until #592 is fixed. |
| A push that fails leaves Keycloak behind the canonical model; nothing retries it. | A failed membership removal keeps the user in the organization. A role assigned while the last role is being unassigned can interleave so that the membership ends while a role remains, or stays while none does. Retrying or reconciling them is [identity-stack#612](https://github.com/jamescrowley321/identity-stack/issues/612). |
| An organization's alias cannot change in Keycloak. | The alias is the canonical tenant id, which does not change either. |
| An organization's domains must be valid domain names, each held by one organization in the realm. Keycloak's domains route a login to an organization's identity provider; they do not add users to it the way Descope's self-provisioning domains do. | A domain Keycloak refuses (400) is an error, and the organization keeps its name and domains. |
| An organization's name must be unique in the realm. | A second tenant with a name another organization holds is refused (409). |
| A role assignment is not mirrored to client roles. Client roles are realm-wide and say nothing of an organization (decision 10); the realm admin grants them and the `type:` markers. | The membership is pushed; the grant is not. |
| A membership can be pushed only once the tenant and the user are both in Keycloak. | Assigning a role in a tenant that was never pushed returns an error. |
| Deleting a role removes its assignments in the database without telling the adapter which users lost their last role in a tenant. | A user whose only role in a tenant was the deleted role keeps the organization membership until it is ended another way: [identity-stack#610](https://github.com/jamescrowley321/identity-stack/issues/610). |

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
| Routes that send the tenant to Descope, parse it as a canonical tenant id or gate by role answer a Keycloak principal 403, `GET /api/tenants` included. | [architecture.md](architecture.md#keycloak-token-model) | Open: the [admin adapter](#admin-adapter) changes no route |
| The realm registers one SPA origin, `http://localhost:3000`, so the Keycloak end-to-end suite runs in a job of its own, apart from the Descope one that serves its SPA there. | CI | None |
| Keycloak sets its session cookies `Secure` on the plain-HTTP origin. Chromium keeps them for a `*.localhost` host; other clients may not, and the login form then answers 400. | Every browser login | None: local and CI only |
