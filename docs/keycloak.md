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
| `backend/tests/integration/test_keycloak_*.py` against the import | Token validation, organizations and client roles, `GET /api/identity` and the organization lookup, in-process | `Integration Tests` |
| The same tests against the Terraform-built realm (`make keycloak-terraform-integration`) | `infra/keycloak/` builds a realm the backend accepts the same way: generated user ids, passwords and directory secret, same results | `Integration Tests` |
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

## Limits found

| Limit | Where it shows | Follow-up |
|---|---|---|
| An organization-bound type is refused to anyone in two organizations, in both (decision 10). `bob` cannot administer either organization. | Scope 7 | By design |
| A portable type carries only the read permissions listed for it. A write permission a user holds on `member` is never granted. | Scope 9 | By design |
| Only `settings:manage` of the `tenant-admin` permissions has a route that checks it. `members:invite` and `members:remove` are in the realm and in tokens and are gated nowhere yet. | Scope 7 | [identity-stack#586](https://github.com/jamescrowley321/identity-stack/issues/586) adds the admin adapter |
| Keycloak does not refuse an authorize request for an organization the user is not in. It issues a token without the `organization` claim, so the backend refuses that token (403), not Keycloak. | Scope 8 | None: the backend refuses |
| A user removed from an organization keeps a portable token's permissions until it expires (300 s with realm defaults). Keycloak refuses the next refresh. An organization-bound token is checked against the directory on every request. | [`keycloak/fixtures/README.md`](../keycloak/fixtures/README.md#leaving-an-organization-while-holding-a-token-for-it) | Recorded (decision 13) |
| Signing out ends the SPA's Keycloak session only. The user's other sessions (another browser, another app client) stay. | Scope 5 | None |
| A silent token renewal can bring back a token for another organization than the one shown. | SPA | [identity-stack#602](https://github.com/jamescrowley321/identity-stack/issues/602) |
| Routes that send the tenant to Descope, parse it as a canonical tenant id or gate by role answer a Keycloak principal 403, `GET /api/tenants` included. | [architecture.md](architecture.md#keycloak-token-model) | [identity-stack#586](https://github.com/jamescrowley321/identity-stack/issues/586) for organizations and users |
| The realm registers one SPA origin, `http://localhost:3000`, so the Keycloak end-to-end suite runs in a job of its own, apart from the Descope one that serves its SPA there. | CI | None |
| Keycloak sets its session cookies `Secure` on the plain-HTTP origin. Chromium keeps them for a `*.localhost` host; other clients may not, and the login form then answers 400. | Every browser login | None: local and CI only |
