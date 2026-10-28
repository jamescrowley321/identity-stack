# Infrastructure providers & credentials

The first artifact toward **all providers under Terraform** with a single secrets
source-of-truth. It inventories every provider, every credential the system
uses, where each one comes from, and what is still manual. Values are never
recorded here — only names and sources.

There are **two separate Terraform roots**, each with its own state:

| Root | Providers | TFC workspace | State in-repo? |
|------|-----------|---------------|----------------|
| `infra/` | descope (fork `jamescrowley321/descope` `~>1.0`), github (`~>6.0`) | `jamescrowley321/identity-stack-dev` | yes (`main.tf` `cloud{}`) |
| `infra/ory/` | ory (`ory/ory` `~>26.3`) | `auth-stack` (comment only) | **no `cloud{}`/`backend{}` block** — effectively local state |

Provider **auth is entirely out-of-band** (hand-set TFC workspace env vars, not
codified): Descope `DESCOPE_MANAGEMENT_KEY`; GitHub `GITHUB_TOKEN`/`GH_TOKEN`;
Ory `ORY_WORKSPACE_API_KEY` + `ORY_WORKSPACE_ID`.

## What TF manages today

- **Descope** (`infra/`): the shared project (imported, `prevent_destroy`), tenants `acme`/`globex`, 12 permissions + 4 roles (`owner`/`admin`/`member`/`viewer`, all imported), access keys `integration_tests` (CI client-creds) + `acme_api`, and the FGA schema.
- **GitHub** (`infra/github.tf`): pushes 7 CI secrets (see table).
- **Ory** (`infra/ory/`): project + config (JWT AT, PKCE), a project admin API key (state-only), the public SPA OAuth2 client, identity schema, optional org.

## Credential inventory

`resource` = always populated by TF · `var` = TF variable · `manual` = set outside TF.

| Secret | Source | TF? | Consumers | Risk |
|--------|--------|-----|-----------|------|
| `DESCOPE_PROJECT_ID` | `var.descope_project_id` (no default) | var | CI, provider auth, backend | required var — can't be empty |
| `DESCOPE_CLIENT_ID` | `descope_access_key.integration_tests.client_id` | resource | CI integ/e2e | ok |
| `DESCOPE_CLIENT_SECRET` | `descope_access_key.integration_tests.cleartext` | resource | CI integ/e2e | ok (sensitive) |
| `DESCOPE_EXPIRED_TOKEN` | `var.descope_expired_token` (sensitive, no default) | var | CI integ (negative auth tests) | ok — static fixture (an expired token stays expired); `validation` + resource `precondition` refuse a blank value. Re-mint only if the project's signing keys change |
| `DESCOPE_MANAGEMENT_KEY` | `descope_management_key.ci_e2e.cleartext` (TF-minted, `infra/management_key.tf`) | resource | CI e2e | ok — always populated; rotate by tainting `descope_management_key.ci_e2e`. Was a `var` defaulting to `""` (the root cause of the E2E gap) until #410. Provider *auth* still uses the hand-set workspace env var — separate |
| `E2E_TEST_EMAIL` | `var.e2e_test_email` (no default) | var | CI e2e | ok — `validation` refuses a blank value |
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
target is: **every credential flows from one source-of-truth (HCP Terraform
variable sets) through Terraform to each provider and consumer, with a guard
that no synced secret can be empty.** (HCP Vault was the earlier plan; it was
dropped because HCP Vault Secrets is end-of-life and HCP Vault Dedicated is a
paid cluster.)

1. **Close the silent-empty class (done).** `#410` repaired the
   descope-provider drift (which had broken `apply` since ~March, the reason the
   secret rotted) and made `DESCOPE_MANAGEMENT_KEY` **self-provisioning** from a
   TF-minted `descope_management_key` resource (`infra/management_key.tf`). Every
   remaining `var`-sourced CI secret (`descope_project_id`, `e2e_test_email`,
   `descope_expired_token`) has no default and a non-empty `validation`, so a
   blank value fails at plan time instead of blanking the secret.
   `DESCOPE_EXPIRED_TOKEN` is no longer minted by a `local-exec` provisioner; it
   is a static sensitive input with an extra `precondition` on the GitHub secret
   (`infra/github.tf`).
2. **Single source-of-truth = HCP Terraform variable sets.** Hold each secret
   once, in a variable set attached to the workspaces that need it (Descope
   auth, Ory auth, org-wide settings); TF fans out to each provider *and* the
   GH-secret sync. No values hand-typed into individual workspaces (the exact
   thing that broke here). The remaining wiring is tracked in #418.
3. **Codify provider auth.** Move Descope/GitHub/Ory provider credentials out of
   per-workspace env vars into those variable sets (#418).
4. **Bring the unmanaged secrets in.** `DESCOPE_WEBHOOK_SECRET`,
   `DESCOPE_FLOW_SYNC_SECRET`, `INTERNAL_IDENTITY_KEY` have *no* source today —
   generate them as TF resources / variable-set entries and deliver to the
   backend.
5. **Link the two roots.** Give `infra/ory/` a real state backend and propagate
   its outputs (issuer, `client_id`, project API key) to backend/frontend config
   automatically, killing the manual-copy drift on `ORY_ISSUER_URL`/`ORY_AUDIENCE`.
