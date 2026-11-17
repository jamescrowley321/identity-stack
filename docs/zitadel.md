# Local Zitadel

A self-hosted [Zitadel](https://zitadel.com) for local development, behind the compose profile `zitadel`. It is disposable: no account, no console step, and a reset returns it to first-start state.

## Run it

```bash
make zitadel-up      # start and wait until healthy (about 30 s after the images are pulled)
make zitadel-reset   # delete the instance and its volumes, then start a fresh one
```

Neither target needs a `.env`, and neither touches the default stack's containers or volumes.

| Service | Image | Role |
|---------|-------|------|
| `zitadel-proxy` | `traefik:v3.7.7` | The only published port, `127.0.0.1:8480`. Routes `/ui/v2/login` to Login V2 and the rest to the API over h2c ([`zitadel/traefik.yml`](../zitadel/traefik.yml)) |
| `zitadel-api` | `ghcr.io/zitadel/zitadel:v4.20.3` | Zitadel, `start-from-init` |
| `zitadel-login` | `ghcr.io/zitadel/zitadel-login:v4.20.3` | Login V2 |
| `zitadel-db` | `postgres:17.10-alpine` | Zitadel's own database, volume `zitadel-pgdata` |

The two Zitadel images are AGPL-3.0 and run unmodified. Traefik is MIT and PostgreSQL is under the PostgreSQL License.

## Issuer

`http://zitadel.localhost:8480`, from the browser and from containers alike. Zitadel answers 404 on any other host, including `http://localhost:8480`.

- **Host and browser:** `*.localhost` resolves to loopback without configuration in Chrome, in curl and in the Linux host resolver (all observed). Where a client does not, add `127.0.0.1 zitadel.localhost` to the hosts file.
- **Containers on the compose network:** the proxy carries the network alias `zitadel.localhost` and listens on 8480 inside the network, so the same URL reaches it.

Two limits, both observed:

- curl inside a container cannot use the URL: it resolves `*.localhost` to loopback itself and never asks Docker's DNS. Python (`urllib`, `httpx`) and BusyBox `wget` resolve the alias.
- py-identity-model 3.18.2 refuses the discovery fetch under its default policy ("HTTPS is required by discovery policy"): the issuer is plain HTTP and `zitadel.localhost` does not count as a loopback address. A backend that validates against this issuer has to opt in to HTTP for it: `ZITADEL_REQUIRE_HTTPS=false` ([below](#the-backend-against-this-instance)).

## The backend against this instance

The backend accepts Zitadel access tokens when `ZITADEL_ISSUER_URL` is set ([environment variables](environment-variables.md)). `terraform -chdir=infra/zitadel output backend_environment` prints the four `ZITADEL_*` values for the applied instance; add the two provider lists:

```bash
ZITADEL_ISSUER_URL=http://zitadel.localhost:8480
ZITADEL_AUDIENCE=<project id>
ZITADEL_CLIENT_ID=<SPA client id>
ZITADEL_REQUIRE_HTTPS=false          # the local issuer is plain HTTP; never off for a real instance
CANONICAL_RBAC_PROVIDERS=ory,zitadel
JIT_ENABLED_PROVIDERS=ory,zitadel
```

Startup fails when the issuer is set without the project id or the client id, and when the issuer is `http` while `ZITADEL_REQUIRE_HTTPS` is on.

**Which tokens are accepted.** A JWT access token is accepted only when its signature verifies against the instance's keys, `iss` is the configured issuer, `aud` contains the project id, `client_id` is the SPA's, and it carries none of `azp`, `at_hash`, `auth_time`, `sid` or `nonce`. Everything else gets the uniform 401: another application's token (its `aud` names the SPA and the project too), the SPA's own ID token (same `aud`, `iss` and `typ`), a machine user's token, an opaque token and a token from another issuer. The last two checks rest on the claim sets recorded from v4.20.3 below; the tests are built from those fixtures, so a re-record after a version change that moves one of the claims fails them.

**Authorization is canonical.** Roles and permissions come from the canonical store; a roles claim on the token is never read, and a deactivated canonical user gets 403 with a valid token. A user in several tenants picks one with the `X-Tenant-ID` header.

**First login.** `GET /api/identity` provisions a user the canonical store does not know. The token's organization (`urn:zitadel:iam:user:resourceowner:id`, present when the SPA requests scope `urn:zitadel:iam:user:resourceowner`) must be linked to exactly one active tenant through `external_org_id`, for the `zitadel` provider (`external_org_provider_id`: the providers' organization ids share the column, and the same string linked for another provider, or for none, is not a link), as `make zitadel-seed` links `acme` and `globex`; the user joins it as `member`. The email's domain must be one of that tenant's `domains` (the `tenants.domains` column, its self-provisioning domains), whole and as written: `globex.test` admits `a@globex.test`, not `a@sub.globex.test`, `a@acme.test@globex.test`, `a@GLOBEX.test` or `a@globex.test.` (an upper-case letter or a trailing dot names the same host, but the lookup by email is exact, so it would be a look-alike second user). Each tenant domain is read lower-cased, without spaces, a leading `@` or trailing dots; one that reads as empty (`""`, `"@"`, `"."`) is dropped, and `domains` that are not a JSON list (null, a string) are read as none and logged at warning with the tenant id. A Descope reconciliation run, which otherwise sets a tenant's domains to the self-provisioning domains of the Descope tenant with its name, leaves a tenant linked to an organization as it is. A tenant with no domains admits no first login. `make zitadel-seed` gives the tenants it creates none, and refuses, writing nothing, to link an existing tenant that already has domains (written by a Descope reconciliation run or `POST /api/tenants` for another purpose); domains written after the seed linked a tenant are kept. No route changes them on an existing tenant, so until an operator writes the domains into `tenants.domains` only the seed's linked user signs in (#621). The email, `email_verified` and the names come from UserInfo, whose `sub` must equal the token's. Self-registration is off in every tenant organization, so only users that organization's administrators or Terraform created can get that membership. No organization, an unlinked organization, an email outside the tenant's domains, a missing or unverified email, or a UserInfo `sub` that differs: nothing is created and the request is refused. An unlinked organization and an email outside the domains get the same 403 `Your organization is not linked to a tenant`; the refusal for the email is logged at warning with the provider, the subject and the tenant id, never the email. The `zitadel` provider row and the organization's link are checked before UserInfo is fetched: no provider row is 500 `Identity provider not configured`. A database seeded before migration 007 has links with no provider, which match no first login until `make zitadel-seed` runs again.

**An email already held is never linked.** A first login is never joined to an existing canonical user by email, whatever that user's tenant or provider, even when UserInfo says the email is verified. Each tenant organization's administrators (`ORG_OWNER`, `ORG_USER_MANAGER`) can create users with any email and mark it verified, so a verified email from Zitadel does not show who the user is. Such a login gets the same 403 as an unlinked organization, as does one whose email is outside the tenant's domains, so the answer does not say which refusal fired. It still says something within the tenant's own domains: an email there that no canonical user holds gets 200, so an organization's administrator who created a user with some email of those domains and signed in as it learns from the 403 that a canonical user holds that email. Outside the domains every email gets the 403, held or not. The refusal is logged at warning with the provider, the subject and the id of the canonical user holding the email, never the email. A Zitadel user who should be an existing canonical user is linked beforehand by an `idp_links` row, as `make zitadel-seed` writes for its user.

The other direction holds too. A user a Zitadel first login creates is marked `email_from_organization`, and no first login of another provider (Ory, Keycloak, the Descope flow sync) and no Descope reconciliation run is ever joined to that user by email, however verified that provider says the email is: the email is one an organization's administrators chose. Otherwise such an administrator could create a Zitadel user with another person's email, and that person's first Ory login would join the administrator's account. That login gets 409 `{"detail": "Could not provision identity"}`, logged the same way.

**The cost: an email in the tenant's domains can be squatted.** The domains keep an organization's administrators to their own tenant's emails: a globex administrator cannot take `ceo@acme.test`. Within those domains they can still create users with their colleagues' emails, mark them verified and sign in once as each. From then on every first login for those emails through another provider, and the Descope flow sync, gets 409, and a Descope reconciliation run skips those Descope users, until an operator acts. Two tenants that list the same domain let either organization take the other's emails: list a domain on one tenant only. The alternative, letting another provider's first login create a second user with the same email, would need the canonical email to stop being unique and is not taken. The operator user list, `GET /api/users`, shows such users with `email_from_organization: true`. To release a squatted email, find the canonical user id in the warning, then either give that user another email (or remove it, if it is not a real user of the organization), so the real person's first login creates their own user; or, when the squatted user is in fact that person, add an `idp_links` row joining their other provider's subject to it, as `make zitadel-seed` does for its user. Then remove the organization user in Zitadel, or take the administrator role from whoever created it.

**Logout** returns the instance's `end_session_endpoint` with `id_token_hint`, the SPA's client id and `ZITADEL_POST_LOGOUT_REDIRECT_URI`, which must be registered on the SPA application.

## Admin adapter

With `SYNC_PROVIDER=zitadel`, a tenant or user created through the API is pushed to Zitadel by `ZitadelSyncAdapter` (`backend/app/services/adapters/zitadel.py`), so nobody runs Terraform for it. The default, `descope`, keeps `DescopeSyncAdapter`. The adapter calls the instance as a machine user with the instance role `IAM_ORG_MANAGER`; `infra/zitadel` creates one (`identity-stack-sync`) and prints its token:

```bash
SYNC_PROVIDER=zitadel
ZITADEL_ADMIN_TOKEN=$(terraform -chdir=infra/zitadel output -raw sync_admin_token)
# ZITADEL_ISSUER_URL and ZITADEL_AUDIENCE (the project id) as above
```

Startup stops when `SYNC_PROVIDER` names no adapter, or names `zitadel` without the issuer, the project id or the token (a value of only whitespace counts as missing, and surrounding whitespace is dropped), or with a `ZITADEL_ADMIN_TIMEOUT_SECONDS` that is not a positive number. The token is a credential: it is never logged, and the refusal names the variable, not its value.

- **A created tenant** gets an organization of the tenant's name, whose Zitadel id is the tenant's id. The adapter gives it a login policy of its own with self-registration off, granted the project with every project role, and only then links the tenant to it (`external_org_id`, and `external_org_provider_id` set to the `zitadel` provider row). A tenant already linked is left alone, and a tenant linked to another provider's organization is refused before Zitadel is called. Organization names are unique in the instance: a tenant whose name another organization holds is refused, and the sync error says so. When a step after the organization's creation fails, the tenant is not linked: the organization stays in Zitadel, named in the logged sync error, and a login into it provisions nobody. Nothing calls the adapter again on its own, but a second sync of that tenant finishes the job: the organization is found by its id (an organization with that id and another name is not the adapter's, and is refused), an own policy closed to registration is kept, a project grant already there is set to every project role, and the tenant is linked last. An organization that already has users when the tenant would be linked is refused and left unlinked: the adapter creates users only after the link, so those users came in another way, for example by self-registration before the policy was closed, and the link would make each of them a tenant member at their first login. Zitadel has no v2 call that sets an organization's login settings, so the policy is written through management v1 (`POST /management/v1/policies/login`). Zitadel zeroes every field a new policy leaves out, so the adapter copies the policy the organization had (the instance default) and changes `allowRegister` alone. That request cannot carry the identity providers linked to the default policy, so when the default links any the sync is refused (the organization is left unlinked) instead of losing them; an own policy that allows self-registration is refused too.
- **A created user** becomes a human user in the tenant's organization, with the canonical user's id as the Zitadel user id, the email as user name and the email left unverified: a tenant administrator typed it in, and Zitadel sends the user a code to verify it. User names are unique in the instance, so an email another Zitadel user has as user name is refused, and the sync error says so. An empty given or family name is sent as the user name, because Zitadel refuses an empty one. The user is recorded as the canonical user's `zitadel` IdP link (`metadata` `{"linked_by": "backend"}`), so their first login finds them, without the email link a first login never makes. A create Zitadel did not answer, or refused because the user already exists, is settled by looking the user up by its id: a user Zitadel has in the tenant's organization is linked, not made again, and one in another organization is refused. Only that create path links. A failed link write is rolled back; the same link written meanwhile by another sync counts as written. No password is set: the user sets one through Zitadel's own flows, which need the instance's email delivery.
- **Deactivating or activating** a canonical user deactivates or reactivates the linked Zitadel user. Deactivation applies from every Zitadel state but inactive (a locked or initial user included); when Zitadel refuses it, the sync error names the user's state. Reactivation applies only to an inactive user. The change reaches the Zitadel user only when it is in the organization of the tenant the status was changed in; a user of another tenant's organization who was given a role in this tenant keeps their Zitadel account as it is (the canonical status still changes), and the sync error names the user's organization. Any other change to the user (profile, removal from a tenant) reaches nothing in Zitadel: a profile edit does not undo an enable or disable made in Zitadel, and a membership is canonical, so a user removed from a tenant stays in the organization it was created in and is granted nothing there. `delete_user` removes the linked Zitadel user; the API has no canonical user delete yet.
- **Roles, permissions and role assignments** stay canonical: those calls return `Ok` and reach nothing. So does a tenant delete.
- **Failures.** Every call answers `Error(SyncError)` and raises nothing, Zitadel unreachable included. As for every adapter, the canonical change stands and the failure is logged at warning; the error's context says `unanswered` when Zitadel gave no readable answer (no response, a timeout, a body that is not JSON). A user with no `zitadel` IdP link (one the adapter did not create and that never logged in) cannot be deactivated or removed in Zitadel: that is a sync error too, not a silent pass.

Inbound events from Zitadel and reconciliation wait for #518. The adapter is proved against the compose instance by `make zitadel-terraform-integration` ([`infra/zitadel/README.md`](../infra/zitadel/README.md#checking-it)).

### Which Zitadel user

The adapter's token may change users in every organization, so it follows only a `zitadel` IdP link that a sign-in made (`{"linked_by": "sign_in"}`) or that the backend wrote: the adapter, for a user it created, or `scripts/seed_zitadel.py` (`{"linked_by": "backend"}`). `POST /api/users/{id}/idp-links` refuses a link that sets that key (422), so a link a tenant admin made, which names whatever Zitadel user the admin typed in, is never followed; links made before this rule carry no origin and are not followed either (a rerun of the seed marks the link it made earlier, when the linked user and the link both have the seed user's email; otherwise the seed is refused). A user whose `zitadel` link is not followed is refused before Zitadel is called: the adapter would act on a Zitadel user that link does not name, and a user has one link per provider. A user with no link is looked up by the canonical user's id. Zitadel lets any user manager choose a new user's id, so that user is deleted only when it is in the organization of one of the canonical user's tenants, has its status changed only when it is in the organization of the tenant the change was made in, and linked only on create, in the organization of the tenant the user was created in; otherwise the sync is refused and names the organization. A status change or delete reaches a user found this way but never links it: a user that a user manager of the organization made with the canonical id would otherwise sign in as the canonical user.

## The SPA against this instance

The SPA has no Zitadel code path: it is pointed at Zitadel by its `VITE_OIDC_*` variables alone ([environment variables](environment-variables.md#frontend-vite--inlined-at-build-time)). `terraform -chdir=infra/zitadel output frontend_environment` prints them for the applied instance:

```bash
VITE_OIDC_AUTHORITY=http://zitadel.localhost:8480
VITE_OIDC_CLIENT_ID=<SPA client id>
VITE_OIDC_REDIRECT_URI=http://localhost:3000
VITE_OIDC_SCOPE=openid profile email offline_access urn:zitadel:iam:user:resourceowner
```

`make zitadel-frontend` builds the compose `frontend` image with those values, read from the Terraform output, and with `VITE_OIDC_TENANT_SCOPE_TEMPLATE` empty, so a template left in `frontend/.env` for another provider does not reach the build. `docker compose up` without `--build` then starts that image; `--build` would rebuild it from the shell and `.env` values. With no applied root the target fails and builds nothing.

- **Sign-in** is authorization code with PKCE (S256), as for every provider. `urn:zitadel:iam:user:resourceowner` puts the user's organization in the access token, and the backend provisions a first login from it; without it the backend refuses the first login. The project-audience scope is not needed: a human's access token already lists the project id in `aud`.
- **Renewal.** `offline_access` gives a refresh token, and the SPA renews the access token with it before it expires (15 minutes on this instance), so a session outlives its access token.
- **Logout** goes through the backend's `logout_url` (above). The SPA drops its tokens first, and Zitadel ends its own session, so the next sign-in asks for credentials again. The access token and the refresh token stay valid until they expire ([End-to-end](#limits-found)).
- **Tenant.** Zitadel puts no tenant in the token. The SPA's tenant switcher chooses among the user's canonical memberships, and every API call names the active one in the `X-Tenant-ID` header; the backend refuses a tenant the caller does not belong to. `GET /api/identity` names none: it is what the memberships come from.
- **A refused first login** (organization not linked, email outside the tenant's domains or already held, no verified email) leaves the SPA with no identity: it shows no tenants, and a role-gated page shows Access Denied.

## End-to-end

[`backend/tests/e2e_zitadel/`](../backend/tests/e2e_zitadel/) drives the shipped SPA and backend against this instance from Chromium, through Login V2's own forms. It needs no credential: CI's `E2E Tests (Zitadel)` job runs it on every pull request. [`backend/tests/integration_zitadel/test_zitadel_machine_token.py`](../backend/tests/integration_zitadel/test_zitadel_machine_token.py) covers the machine user's token in `make zitadel-terraform-integration` (CI's `Zitadel Terraform Check`).

| Case | Proved |
|------|--------|
| First login | The fixture's one-tenant member (`alice`, in `Acme`) signs in through the SPA; `GET /api/identity` returns a new canonical user, a `member` of `acme` and of nothing else, linked to its Zitadel id |
| Roles and tenant switch | The seeded two-tenant user (`bob`) is `admin` in `acme` (`GET /api/access-checks/admin-role` 200). After the SPA's switcher picks `globex`, the SPA's calls name it in `X-Tenant-ID`, the same route answers 403 and `GET /api/access-checks/viewer-role` 200 |
| Unmapped organization | The fixture's user in `Unlinked` (`grace`) signs in; `GET /api/identity` answers 403 `Your organization is not linked to a tenant`, no canonical user is created, the SPA shows "No tenants" and a gated page Access Denied |
| Self-registration | Login V2's register page refuses in `acme` and `globex`: "The registration is disabled", no form |
| Token binding | A live SPA ID token and a live access token of the project's second application each get the uniform 401. A machine user's token passes discovery, the JWKS, the signature, the issuer and the audience (the project id) and still gets 401 from the client binding |
| Logout | The SPA holds no tokens, and the next sign-in from a protected page asks for credentials. The suite records, without asserting, whether the old refresh token and access token are still accepted |

Run it with the instance applied and seeded, the backend and the SPA up:

```bash
make zitadel-reset && make test-up
cd infra/zitadel && export TF_VAR_zitadel_access_token="$(make -s -C ../.. zitadel-admin-pat)" \
  && terraform init && terraform apply -var-file=environments/dev.tfvars && cd ../..
export DATABASE_URL=postgresql+asyncpg://identity_test:identity_test@localhost:15432/identity_test
(cd backend && python -m alembic upgrade head) && make zitadel-seed
# the SPA: npm run build with `terraform -chdir=infra/zitadel output frontend_environment`
#   and VITE_OIDC_TENANT_SCOPE_TEMPLATE empty, then `npx vite preview --port 3000 --strictPort`
# the backend on :8000: `output backend_environment`, CANONICAL_RBAC_PROVIDERS=zitadel,
#   JIT_ENABLED_PROVIDERS=zitadel, FRONTEND_URL=http://localhost:3000, DESCOPE_PROJECT_ID= and
#   RATE_LIMIT_DEFAULT=2000/minute RATE_LIMIT_AUTH=1000/minute
make test-e2e-zitadel
```

The target hands the suite the applied root's outputs (`terraform -chdir=infra/zitadel output -json`) in `ZITADEL_TERRAFORM_OUTPUTS`. A missing output, an instance, database, backend or SPA that does not answer, or a tenant that is not linked fails every test; nothing skips. The first-login case needs a database where `alice` has not signed in yet: after a run, start again from `make zitadel-reset`. `E2E_BACKEND_URL` and `E2E_FRONTEND_URL` point the suite at other addresses; the SPA's origin must be the root's `frontend_url`. The CI job in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs these steps.

### Limits found

Measured on v4.20.3 by the suite.

- **An unexpired access token outlives logout.** The backend validates a JWT access token locally, against the instance's keys, so the access token the SPA held before logout is still accepted (`GET /api/identity` 200 after logout) until it expires, 15 minutes after issue on this instance. Nothing revokes it before then until identity-stack#542 (revocation before expiry) exists.
- **The refresh token outlives logout.** Logout ends the Zitadel session (`end_session` with `id_token_hint`), but the refresh token issued for `offline_access` is not revoked with it: the token endpoint still exchanges it after logout (200). Anyone holding a copy can mint new access tokens until it expires (`refresh_token_idle_expiration` 30 days, `refresh_token_expiration` 90 days). The SPA keeps its tokens in `sessionStorage` and drops them at logout; revoking the refresh token at logout waits for identity-stack#542.
- **Login V2 remembers who signed in.** After logout the next sign-in opens Login V2's account list ("Select the account you want to use") with the user's login name, from Login V2's own cookie. Choosing it asks for the password: the session is gone, the login name is not.
- **The tenants' email domains are written by hand.** `make zitadel-seed` gives the tenants no `domains`, and a first login is admitted only for an email in its tenant's domains. The suite writes `acme.test` into the `acme` tenant's `domains` itself, as an operator would, and puts the tenant's own domains back when it ends, until identity-stack#621 gives operators a way to set them.
- **Few routes serve a Zitadel principal.** Routes that call Descope refuse every other provider's principal, and the IdP-link routes read Descope's `dct` claim for their tenant (identity-stack#590), so the role-gated routes a Zitadel admin can reach today are the access checks.

## Bootstrap

First start creates everything below from `ZITADEL_FIRSTINSTANCE_*` settings. Anything else inside the instance belongs to Terraform: [`infra/zitadel/`](../infra/zitadel/README.md) declares the organizations, the project and its applications, the test users, registration off and the 15-minute access-token lifetime, and `make zitadel-seed` links the tenant organizations to canonical tenants.

| What | Value |
|------|-------|
| Organization | `ZITADEL` |
| Admin machine user | `terraform-admin`, `IAM_OWNER`. Its personal access token is written to `/zitadel/bootstrap/admin.pat` on the volume `zitadel-bootstrap`; `make -s zitadel-admin-pat` prints it |
| Login V2 service user | `login-client`; its token is on the same volume |
| Human admin | `zitadel-admin@zitadel.zitadel.localhost` / `Local-only-Passw0rd!`, `IAM_OWNER`, email verified |

**Every secret in the compose file is a local-only throwaway**: the masterkey, the Login V2 cookie secret, the database password and the human admin's password. They are committed on purpose, protect nothing, and must not be reused anywhere. Do not run the profile on a shared host: the human admin is `IAM_OWNER` behind a password anyone can read here. The tokens on the volume are minted per instance and die with `make zitadel-reset`.

## What the instance does

Recorded from v4.20.3 on 2026-11-15 by [`scripts/zitadel-capture-fixtures.py`](../scripts/zitadel-capture-fixtures.py) into [`backend/tests/fixtures/zitadel/`](../backend/tests/fixtures/zitadel/). Ids, key ids and timestamps differ on every instance; the shapes do not. Signed tokens are not stored, only their decoded header and claims.

| Question | Observed | Fixture |
|----------|----------|---------|
| Issuer string | `http://zitadel.localhost:8480`: scheme, external domain and external port, no trailing slash. The `iss` of every token is the same string | `discovery.json` |
| Signing keys | Two RSA keys, `RS256`. Token headers carry `alg`, `kid` and `typ: JWT` | `jwks.json` |
| Default access-token type | Opaque, for an application and for a machine user created without naming a type. JWT has to be asked for: `OIDC_TOKEN_TYPE_JWT` on the application, `ACCESS_TOKEN_TYPE_JWT` on the machine user | `defaults.json` |
| `aud`, machine user, client credentials | Scope `openid` alone: the machine user's own client id (its username). With `urn:zitadel:iam:org:project:id:{projectid}:aud`: the project id **only**; the client id is gone | `machine-access-token-openid.json`, `machine-access-token-project.json` |
| `aud`, human through an application | The client id of every application in the project, then the project id, with no audience scope requested. The project-audience scope adds nothing | `human-access-token-openid.json`, `human-access-token-project.json` |
| Shape of `urn:zitadel:iam:org:project:roles` | An object, not an array: role key → organization id → organization primary domain, e.g. `{"viewer": {"<org id>": "zitadel.zitadel.localhost"}}` | `human-access-token-roles.json` |
| When the roles claim is in a JWT access token | Human: only when the application sets `accessTokenRoleAssertion`; then both `urn:zitadel:iam:org:project:roles` and `urn:zitadel:iam:org:project:{projectid}:roles` appear. Without it there is no roles claim even when `urn:zitadel:iam:org:projects:roles` is requested and a grant exists; UserInfo returns both. Machine user with that scope: the project-specific claim only | `human-access-token-roles.json`, `human-access-token-project.json`, `human-userinfo-project.json`, `machine-access-token-project.json` |
| Organization claim | `urn:zitadel:iam:user:resourceowner:id`, `:name` and `:primary_domain`, only when scope `urn:zitadel:iam:user:resourceowner` is requested | `human-access-token-project.json` |
| `email` and `email_verified` in a JWT access token | Absent, for a human with a verified email and scopes `openid profile email`. Absent from the ID token too, by default. UserInfo returns both | `human-access-token-project.json`, `human-id-token-project.json`, `human-userinfo-project.json` |
| DPoP | Not advertised: no `dpop_signing_alg_values_supported` | `discovery.json` |
| Mutual TLS | Not advertised: no `mtls_endpoint_aliases`, no `tls_client_certificate_bound_access_tokens`, no `tls_client_auth` method | `discovery.json` |
| PAR | Not advertised: no `pushed_authorization_request_endpoint` | `discovery.json` |
| Other discovery facts | No `authorization_details_types_supported` (RAR). Token exchange is not among `grant_types_supported`. `backchannel_logout_supported` is true. `code_challenge_methods_supported` is `S256` only | `discovery.json` |

The human tokens are for the first-instance admin. `defaults.json` holds no claims because both default tokens were opaque.

### Re-recording

```bash
make zitadel-reset
make -s zitadel-admin-pat | python3 scripts/zitadel-capture-fixtures.py
make zitadel-reset
```

The script creates a project, a role, three applications and two machine users to get tokens, hence the reset after it.
