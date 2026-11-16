# Infrastructure providers & credentials

The first artifact toward **all providers under Terraform** with a single secrets
source-of-truth. It inventories every provider, every credential the system
uses, where each one comes from, and what is still manual. Values are never
recorded here — only names and sources.

There are **three separate Terraform roots**, each with its own state:

| Root | Providers | TFC workspace | State in-repo? |
|------|-----------|---------------|----------------|
| `infra/` | descope (fork `jamescrowley321/descope` `~>1.0`), github (`~>6.0`), local | `jamescrowley321/identity-stack-dev` | yes (`main.tf` `cloud{}`) |
| `infra/ory/` | ory (`ory/ory` `~>26.3`) | `auth-stack` (comment only) | **no `cloud{}`/`backend{}` block** — effectively local state |
| `infra/keycloak/` | keycloak (`keycloak/keycloak` `~>5.10`), random (`hashicorp/random` `~>3.6`) | none | **no** — local state, git-ignored; targets the compose Keycloak only |

Provider **auth is entirely out-of-band** (hand-set TFC workspace env vars, not
codified): Descope `DESCOPE_MANAGEMENT_KEY`; GitHub `GITHUB_TOKEN`/`GH_TOKEN`;
Ory `ORY_WORKSPACE_API_KEY` + `ORY_WORKSPACE_ID`; Keycloak `KEYCLOAK_USER` +
`KEYCLOAK_PASSWORD` (the compose container's bootstrap admin, a local fixture login set by
`KC_BOOTSTRAP_ADMIN_*` in `docker-compose.yml`), exported in the shell that runs Terraform. No
tfvars file holds a credential.

## What TF manages today

- **Descope** (`infra/`): the shared project (imported, `prevent_destroy`), tenants `acme`/`globex`, 12 permissions + 4 roles (`owner`/`admin`/`member`/`viewer`, all imported), access keys `integration_tests` (CI client-creds) + `acme_api`, and the FGA schema.
- **GitHub** (`infra/github.tf`): pushes 7 CI secrets (see table) + generates `DESCOPE_EXPIRED_TOKEN` via a `local-exec` curl.
- **Ory** (`infra/ory/`): project + config (JWT AT, PKCE), a project admin API key (state-only), the public SPA OAuth2 client, identity schema, optional org.
- **Keycloak** (`infra/keycloak/`): the identity-stack realm the compose import ships — organizations `acme`/`globex` and their memberships, type clients and client roles, PKCE app clients, fixture users, and the read-only `identity-stack-directory` service account. See [`keycloak/README.md`](keycloak/README.md).

## Credential inventory

`resource` = always populated by TF · `var` = TF variable · `manual` = set outside TF.

| Secret | Source | TF? | Consumers | Risk |
|--------|--------|-----|-----------|------|
| `DESCOPE_PROJECT_ID` | `var.descope_project_id` (no default) | var | CI, provider auth, backend | required var — can't be empty |
| `DESCOPE_CLIENT_ID` | `descope_access_key.integration_tests.client_id` | resource | CI integ/e2e | ok |
| `DESCOPE_CLIENT_SECRET` | `descope_access_key.integration_tests.cleartext` | resource | CI integ/e2e | ok (sensitive) |
| `DESCOPE_EXPIRED_TOKEN` | `local-exec` curl → `local_file` | resource (derived) | CI integ | **fragile** — local curl/python + 3-min expiry; empty on failure |
| `DESCOPE_MANAGEMENT_KEY` | `descope_management_key.ci_e2e.cleartext` (TF-minted) | resource | CI e2e, backend | ✅ fixed in #410 — was `var` default `""` (the root cause of the E2E gap); now self-provisioning, always valid. (Provider *auth* still uses the workspace env var — separate.) |
| `E2E_TEST_EMAIL` | `var.e2e_test_email` (default now synthetic) | var | CI e2e | ✅ fixed in #410 — default set to a synthetic non-deliverable address; no longer blanks |
| `E2E_TEST_TENANT_ID` | `descope_tenant.acme.id` | resource | CI e2e | ok |
| GitHub provider token | manual TFC env var | manual | `terraform apply` | not codified |
| `ORY_WORKSPACE_API_KEY` | manual env / TFC var | manual | Ory provider auth | not in TF; rotation pending (#375) |
| `ory_project_api_key.tf` | `ory_project_api_key` resource | resource | (intended) backend OrySyncAdapter | **state-only; no route to runtime** |
| Ory SPA `client_id` | `ory_oauth2_client.spa.client_id` | resource | frontend `VITE_OIDC_CLIENT_ID` | **not wired into frontend yet** |
| `ORY_ISSUER_URL` / `ORY_AUDIENCE` | app env (from Ory outputs / `var.spa_audience`) | manual | backend middleware | **manual copy → drift breaks audience validation** |
| `DESCOPE_WEBHOOK_SECRET` | app env | **none** | backend `/internal` | **no TF/GH source** (fail-closed, warn-only) |
| `DESCOPE_FLOW_SYNC_SECRET` | app env | **none** | backend `/internal` | **no TF/GH source** |
| `INTERNAL_IDENTITY_KEY` | app env | **none** | backend `/internal` | **no TF/GH source** |
| Keycloak provider login (`KEYCLOAK_USER` / `KEYCLOAK_PASSWORD`) | shell env; the compose bootstrap admin (`admin`/`admin`, local fixture) | manual | `infra/keycloak/` provider auth | local container only |
| `KEYCLOAK_DIRECTORY_CLIENT_SECRET` | compose import: `keycloak/realm/identity-stack-realm.json`, client `identity-stack-directory` (local fixture value `local-only-directory-client`); Terraform realm: `keycloak_openid_client.directory.client_secret`, output `keycloak_directory_client_secret` (generated by Keycloak) | resource (Terraform realm) | backend organization lookup (`services/keycloak_directory.py`) | read-only by role (`view-users`, `view-organizations`); state-only, copied to the backend by hand |
| Keycloak fixture user passwords (Terraform realm) | `random_password.user`, output `user_passwords` | resource | local logins, tests against the Terraform realm | state-only |
| `TYK_GATEWAY_SECRET` | compose / `tyk.conf` | manual | gateway profile | must match `tyk.conf` |
| `POSTGRES_PASSWORD` / `REDIS_PASSWORD` / `DATABASE_URL` | compose env | manual | local stack | not in TF |

## Gaps → roadmap to "all providers under TF"

The failure mode this document was born from — a `var` with `default = ""`
silently overwriting a live CI secret with an empty string — generalises. The
target is: **every credential flows from one source-of-truth (HCP Vault) through
Terraform to each provider and consumer, with a guard that no synced secret can
be empty.**

1. **Close the silent-empty class (largely done).** `#410` repaired the
   descope-provider drift (which had broken `apply` since ~March, the reason the
   secret rotted) and made `DESCOPE_MANAGEMENT_KEY` **self-provisioning** from a
   TF-minted `descope_management_key` resource; `E2E_TEST_EMAIL` now defaults to a
   synthetic address. `#396` adds `github.tf` preconditions + a CI coverage gate.
   Still to do: source `DESCOPE_EXPIRED_TOKEN` the same way (it can still push
   empty on a `local-exec` failure).
2. **Single source-of-truth = HCP Vault.** Read every secret from Vault via the
   `vault` provider; TF fans out to each provider *and* the GH-secret sync. No
   hand-set TFC workspace variables (the exact thing that broke here).
3. **Codify provider auth.** Bring Descope/GitHub/Ory provider credentials under
   Vault-sourced config instead of out-of-band workspace env vars.
4. **Bring the unmanaged secrets in.** `DESCOPE_WEBHOOK_SECRET`,
   `DESCOPE_FLOW_SYNC_SECRET`, `INTERNAL_IDENTITY_KEY` have *no* source today —
   generate them as TF resources / Vault entries and deliver to the backend.
5. **Link the two roots.** Give `infra/ory/` a real state backend and propagate
   its outputs (issuer, `client_id`, project API key) to backend/frontend config
   automatically, killing the manual-copy drift on `ORY_ISSUER_URL`/`ORY_AUDIENCE`.
6. **De-fragilise `DESCOPE_EXPIRED_TOKEN`** — replace the `local-exec` curl with a
   provider/data-source approach or a stable fixture.
