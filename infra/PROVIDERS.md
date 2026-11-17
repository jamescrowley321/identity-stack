# Infrastructure providers & credentials

The first artifact toward **all providers under Terraform** with a single secrets
source-of-truth. It inventories every provider, every credential the system
uses, where each one comes from, and what is still manual. Values are never
recorded here — only names and sources.

There are **four separate Terraform roots**, each with its own state:

| Root | Providers | TFC workspace | State in-repo? |
|------|-----------|---------------|----------------|
| `infra/` | descope (fork `jamescrowley321/descope` `~>1.0`), github (`~>6.0`), local | `jamescrowley321/identity-stack-dev` | yes (`main.tf` `cloud{}`) |
| `infra/ory/` | ory (`ory/ory` `~>26.3`) | `auth-stack` (comment only) | **no `cloud{}`/`backend{}` block** — effectively local state |
| `infra/keycloak/` | keycloak (`keycloak/keycloak` `~>5.10`), random (`hashicorp/random` `~>3.6`) | none | **no** — local state, git-ignored; targets the compose Keycloak only |
| `infra/zitadel/` | zitadel (`zitadel/zitadel` `~>3.8`), random (`hashicorp/random` `~>3.6`) | none | **no** — local state (`infra/zitadel/terraform.tfstate`), git-ignored; targets the compose Zitadel only |

Provider **auth is entirely out-of-band** (hand-set TFC workspace env vars, not
codified): Descope `DESCOPE_MANAGEMENT_KEY`; GitHub `GITHUB_TOKEN`/`GH_TOKEN`;
Ory `ORY_WORKSPACE_API_KEY` + `ORY_WORKSPACE_ID`; Keycloak `KEYCLOAK_USER` +
`KEYCLOAK_PASSWORD` (the compose container's bootstrap admin, a local fixture login set by
`KC_BOOTSTRAP_ADMIN_*` in `docker-compose.yml`); Zitadel `TF_VAR_zitadel_access_token` (the
compose bootstrap admin machine user's personal access token, `make -s zitadel-admin-pat`),
exported in the shell that runs Terraform. No tfvars file holds a credential.

## What TF manages today

- **Descope** (`infra/`): the shared project (imported, `prevent_destroy`), tenants `acme`/`globex`, 12 permissions + 4 roles (`owner`/`admin`/`member`/`viewer`, all imported), access keys `integration_tests` (CI client-creds) + `acme_api`, and the FGA schema.
- **GitHub** (`infra/github.tf`): pushes 7 CI secrets (see table) + generates `DESCOPE_EXPIRED_TOKEN` via a `local-exec` curl.
- **Ory** (`infra/ory/`): project + config (JWT AT, PKCE), a project admin API key (state-only), the public SPA OAuth2 client, identity schema, optional org.
- **Shared test fixture** (`infra/fixtures/test-identities.json`): the test tenants, users, memberships and canonical roles. `infra/keycloak` and `infra/zitadel` build theirs from it, and the backend's Keycloak and Zitadel suites take user names from it. It records the Descope tenant each tenant maps to; the Descope and Ory roots are not built from it. See [`fixtures/README.md`](fixtures/README.md).
- **Keycloak** (`infra/keycloak/`): the identity-stack realm the compose import ships — the shared fixture's organizations and memberships, type clients and client roles, PKCE app clients, fixture users, the read-only `identity-stack-directory` service account, and the `identity-stack-admin` service account the backend's Keycloak admin adapter writes with. See [`keycloak/README.md`](keycloak/README.md). Checked in CI against Keycloak 26.8.2: `make keycloak-terraform-check` compares the built realm with the import, and `make keycloak-terraform-integration` runs the backend's Keycloak integration tests against it, which pass as they do against the import. Results and limits: [`docs/keycloak.md`](../docs/keycloak.md).
- **Zitadel** (`infra/zitadel/`): everything inside the compose Zitadel — instance login policy (registration off) and OIDC settings (15-minute access tokens), organization `identity-stack` and one per fixture tenant (`Acme`/`Globex`/`Unlinked`) with their own login policies, the `identity-stack` project with roles `owner`/`admin`/`member`/`viewer` granted to nobody, project grants to the two tenants, the SPA and a second OIDC application, a test machine user, and one human per fixture user. `make zitadel-seed` links it to canonical tenants. See [`zitadel/README.md`](zitadel/README.md).

## Common variables and outputs of the local provider roots

`infra/keycloak` and `infra/zitadel` take and give the same names, so wiring the app to either
provider is the same two commands: apply the root, then `make <provider>-env`.

| Variable | What |
|---|---|
| `issuer_url` | The provider's issuer, exactly as in a token's `iss`. Keycloak: `<KC_HOSTNAME>/realms/<realm>`, the realm the root creates. Zitadel: the instance's base URL. |
| `frontend_url` | The SPA's origin, a bare `scheme://host[:port]`: the redirect URI, and `<frontend_url>/login` after logout. |

Plain `http://` in either, and Keycloak's `ssl_required = "none"`, is accepted only for a
loopback or `*.localhost` host. That rule is written once, in
[`modules/provider-urls`](modules/provider-urls/variables.tf), and both roots read their URLs
through that module, so a refused URL fails at `terraform plan` before the provider is called.

| Output | What |
|---|---|
| `backend_environment` (sensitive) | A map whose keys are exactly the backend environment variables for the provider ([`docs/environment-variables.md`](../docs/environment-variables.md)), service-account secrets included: it alone configures the backend for that provider. Sensitive for those secrets. They include the admin adapter's credential, which writes to the provider (`KEYCLOAK_ADMIN_CLIENT_SECRET`: `manage-users`, `manage-organizations`; `ZITADEL_ADMIN_TOKEN`: `IAM_ORG_MANAGER`). A backend that does not sync to the provider (`SYNC_PROVIDER` names another) needs neither: the e2e jobs leave them out. |
| `frontend_environment` | The same for the SPA: the `VITE_OIDC_*` variables of one build. |
| `test_credentials` (sensitive) | What tests log in with: `users`, each fixture user's password keyed by fixture user, and `clients`, each test-only client's secret keyed by client id (empty when a root has none). |

`make keycloak-env` and `make zitadel-env` print the two environment maps of the applied root in
`.env` form, under `# backend/.env` and `# frontend/.env`. Values are single-quoted, so a shell
can source the lines too. The roots' check scripts, the Keycloak integration run against the
Terraform realm (`make keycloak-terraform-integration`) and the Keycloak and Zitadel e2e set-up
(the CI jobs, `make test-e2e-keycloak`, `make test-e2e-zitadel`) and Zitadel integration set-up
read these outputs. `tests/unit/test_provider_terraform_roots.py` fails when a root
lacks one of them or a variable, when a key of an environment map is not in the matching table
of `docs/environment-variables.md`, or when a root writes its own plain-HTTP rule.

Each root keeps outputs of its own beside these (user ids, client ids, Zitadel's organization ids
and `canonical_seed`). The hosted roots, Descope (`infra/`) and Ory (`infra/ory/`), do not have
these outputs: adding them would change their HCP Terraform workspaces.

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
| `KEYCLOAK_DIRECTORY_CLIENT_SECRET` | compose import: `keycloak/realm/identity-stack-realm.json`, client `identity-stack-directory` (local fixture value `local-only-directory-client`); Terraform realm: `keycloak_openid_client.directory.client_secret`, `KEYCLOAK_DIRECTORY_CLIENT_SECRET` of output `backend_environment` (generated by Keycloak) | resource (Terraform realm) | backend organization lookup (`services/keycloak_directory.py`) | read-only by role (`view-users`, `view-organizations`); state-only, copied to the backend by hand |
| `KEYCLOAK_ADMIN_CLIENT_SECRET` | compose import: `keycloak/realm/identity-stack-realm.json`, client `identity-stack-admin` (local fixture value `local-only-admin-client`); Terraform realm: `keycloak_openid_client.admin.client_secret`, `KEYCLOAK_ADMIN_CLIENT_SECRET` of output `backend_environment` (generated by Keycloak) | resource (Terraform realm) | backend Keycloak admin adapter (`services/keycloak_admin.py`, `SYNC_PROVIDER=keycloak`) | writes users and organizations by role (`manage-users`, `manage-organizations`), nothing else; state-only, copied to the backend by hand |
| Keycloak fixture user passwords (Terraform realm) | `random_password.user`, output `test_credentials` (`users`) | resource | local logins, tests against the Terraform realm | state-only |
| Zitadel provider token (`TF_VAR_zitadel_access_token`) | shell env; the compose bootstrap admin machine user `terraform-admin` (`IAM_OWNER`), written by first start to the `zitadel-bootstrap` volume, printed by `make -s zitadel-admin-pat` | manual | `infra/zitadel/` provider auth | local container only; minted per instance, dies with `make zitadel-reset` |
| Zitadel test user passwords | `random_password.user`, output `test_credentials` (`users`) | resource | local logins, tests against the compose Zitadel | state-only |
| Zitadel test machine user secret | `zitadel_machine_user.tests.client_secret`, output `test_credentials` (`clients`, keyed by its client id; generated by Zitadel) | resource | client credentials in tests | state-only |
| Zitadel SPA client id | `zitadel_application_oidc.spa.client_id`, outputs `spa_client_id`, `frontend_environment`, `backend_environment` | resource | frontend `VITE_OIDC_CLIENT_ID`, backend `ZITADEL_CLIENT_ID` | not a secret; printed by `make zitadel-env` |
| `ZITADEL_ADMIN_TOKEN` | `zitadel_personal_access_token.sync.token` of the machine user `identity-stack-sync` (`IAM_ORG_MANAGER`), `ZITADEL_ADMIN_TOKEN` of output `backend_environment` | resource | backend Zitadel admin adapter (`SYNC_PROVIDER=zitadel`) | state-only; printed by `make zitadel-env`; dies with `make zitadel-reset` |
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
