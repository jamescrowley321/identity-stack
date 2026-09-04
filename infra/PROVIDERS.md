# Infrastructure providers & credentials

The first artifact toward **all providers under Terraform** with a single secrets
source-of-truth. It inventories every provider, every credential the system
uses, where each one comes from, and what is still manual. Values are never
recorded here — only names and sources.

There are **two separate Terraform roots**, each with its own state:

| Root | Providers | TFC workspace | State in-repo? |
|------|-----------|---------------|----------------|
| `infra/` | descope (fork `jamescrowley321/descope` `~>1.0`), github (`~>6.0`), local | `jamescrowley321/identity-stack-dev` | yes (`main.tf` `cloud{}`) |
| `infra/ory/` | ory (`ory/ory` `~>26.3`) | `auth-stack` (comment only) | **no `cloud{}`/`backend{}` block** — effectively local state |

Provider **auth is entirely out-of-band** (hand-set TFC workspace env vars, not
codified): Descope `DESCOPE_MANAGEMENT_KEY`; GitHub `GITHUB_TOKEN`/`GH_TOKEN`;
Ory `ORY_WORKSPACE_API_KEY` + `ORY_WORKSPACE_ID`.

## What TF manages today

- **Descope** (`infra/`): the shared project (imported, `prevent_destroy`), tenants `acme`/`globex`, 12 permissions + 4 roles (`owner`/`admin`/`member`/`viewer`, all imported), access keys `integration_tests` (CI client-creds) + `acme_api`, and the FGA schema.
- **GitHub** (`infra/github.tf`): pushes 7 CI secrets (see table) + generates `DESCOPE_EXPIRED_TOKEN` via a `local-exec` curl.
- **Ory** (`infra/ory/`): project + config (JWT AT, PKCE), a project admin API key (state-only), the public SPA OAuth2 client, identity schema, optional org.

## Credential inventory

`resource` = always populated by TF · `var` = TF variable · `manual` = set outside TF.

| Secret | Source | TF? | Consumers | Risk |
|--------|--------|-----|-----------|------|
| `DESCOPE_PROJECT_ID` | `var.descope_project_id` (no default) | var | CI, provider auth, backend | required var — can't be empty |
| `DESCOPE_CLIENT_ID` | `descope_access_key.integration_tests.client_id` | resource | CI integ/e2e | ok |
| `DESCOPE_CLIENT_SECRET` | `descope_access_key.integration_tests.cleartext` | resource | CI integ/e2e | ok (sensitive) |
| `DESCOPE_EXPIRED_TOKEN` | `local-exec` curl → `local_file` | resource (derived) | CI integ | **fragile** — local curl/python + 3-min expiry; empty on failure |
| **`DESCOPE_MANAGEMENT_KEY`** | **`var.descope_management_key` (default `""`)** | var | CI e2e, backend, **provider auth** | **⚠ default `""` → silent empty secret** (root cause of the E2E gap) |
| **`E2E_TEST_EMAIL`** | **`var.e2e_test_email` (default `""`)** | var | CI e2e | **⚠ default `""` → silent empty secret** |
| `E2E_TEST_TENANT_ID` | `descope_tenant.acme.id` | resource | CI e2e | ok |
| `OPENROUTER_API_KEY` | manual GH secret | manual | adversarial-review lenses | not in TF |
| GitHub provider token | manual TFC env var | manual | `terraform apply` | not codified |
| `ORY_WORKSPACE_API_KEY` | manual env / TFC var | manual | Ory provider auth | not in TF; rotation pending (#375) |
| `ory_project_api_key.tf` | `ory_project_api_key` resource | resource | (intended) backend OrySyncAdapter | **state-only; no route to runtime** |
| Ory SPA `client_id` | `ory_oauth2_client.spa.client_id` | resource | frontend `VITE_OIDC_CLIENT_ID` | **not wired into frontend yet** |
| `ORY_ISSUER_URL` / `ORY_AUDIENCE` | app env (from Ory outputs / `var.spa_audience`) | manual | backend middleware | **manual copy → drift breaks audience validation** |
| `DESCOPE_WEBHOOK_SECRET` | app env | **none** | backend `/internal` | **no TF/GH source** (fail-closed, warn-only) |
| `DESCOPE_FLOW_SYNC_SECRET` | app env | **none** | backend `/internal` | **no TF/GH source** |
| `INTERNAL_IDENTITY_KEY` | app env | **none** | backend `/internal` | **no TF/GH source** |
| `TYK_GATEWAY_SECRET` | compose / `tyk.conf` | manual | gateway profile | must match `tyk.conf` |
| `POSTGRES_PASSWORD` / `REDIS_PASSWORD` / `DATABASE_URL` | compose env | manual | local stack | not in TF |

## Gaps → roadmap to "all providers under TF"

The failure mode this document was born from — a `var` with `default = ""`
silently overwriting a live CI secret with an empty string — generalises. The
target is: **every credential flows from one source-of-truth (HCP Vault) through
Terraform to each provider and consumer, with a guard that no synced secret can
be empty.**

1. **Close the silent-empty class (done / in flight).** `#396` adds
   `github.tf` preconditions (fail `apply` on empty `descope_management_key` /
   `e2e_test_email`) + a CI coverage gate. Do the same for `DESCOPE_EXPIRED_TOKEN`.
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
