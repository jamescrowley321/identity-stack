# Ory Network IaC (`infra/ory`)

Terraform that provisions **Ory Network** as a configurable OIDC provider for identity-stack,
parallel to the existing Descope track in `infra/`. Uses the official `ory/ory` provider
(v26.3.x). The Descope Terraform (`infra/*.tf`) is untouched.

Framing: make identity-stack's IdP **configurable** so Ory can be configured and run — not a
Descope swap-out. See the planning docs in `identity-stack-planning`
(`docs/ory-sso-provider-context.md`, `docs/ory-iac-automation-plan.md`,
`_bmad-output/planning-artifacts/epics-ory-sso-provider.md`).

## What this creates (reusable module → one Ory project per instance)

`modules/ory-oidc-app` provisions, per instance:

| Resource | Purpose |
|---|---|
| `ory_project` | The Ory project (OAuth2/OIDC via Ory Hydra). |
| `ory_project_config` | JWT access-token strategy (`oauth2_strategies_access_token = "jwt"`) + PKCE enforced for public clients. |
| `ory_project_api_key` | Terraform-managed **project** API key — used to create the OAuth2 client, and later by the backend `OrySyncAdapter`. Value lives only in state. |
| `ory_oauth2_client` | Public **SPA** client: `authorization_code` + `refresh_token`, PKCE, `token_endpoint_auth_method = none`. |
| `ory_identity_schema` | SCIM-aligned default schema: `email` (identifier), `given_name`, `family_name`. |
| `ory_organization` | Optional B2B org (`enable_organizations`, default **false** → canonical-side tenancy). |

### Live values — `identity-stack-dev` (Ory workspace `auth-stack`, Development/free tier)

| Field | Value |
|---|---|
| Ory workspace | `auth-stack` — `1a710b61-9aaa-473c-aab5-77b5a5f645ad` |
| Project | `identity-stack-dev` — `91ab168c-0dcc-41f5-a46a-67194caac7ad` |
| Slug | `inspiring-nash-yli2uiwmcw` |
| Issuer | `https://inspiring-nash-yli2uiwmcw.projects.oryapis.com` |
| Discovery | `…/.well-known/openid-configuration` |
| SPA `client_id` | `58142046-5beb-420e-a4cd-310f7263357f` |

These feed the follow-on epics (no secrets among them):
- **Backend** (Epic 2): register an `ory` provider row with `issuer_url` = the issuer above; add it to the middleware issuer allow-list.
- **Frontend** (Epic 4): `VITE_OIDC_AUTHORITY` = issuer, `VITE_OIDC_CLIENT_ID` = the `client_id`, `VITE_OIDC_SCOPE = "openid profile email offline_access"`.
- **Logout** (Epic 5): `end_session_endpoint` from discovery.

## Credentials

| Credential | Used for | Home |
|---|---|---|
| **Workspace API key** (`ory_wak_…`) | Terraform provider auth (create/configure projects) | GitHub secret `ORY_WORKSPACE_API_KEY` on this repo (+ `ORY_WORKSPACE_ID` variable). **Interim** — move to HCP workspace vars / Infisical. |
| **Project API key** (`ory_pat_…`) | The SPA-client resource + backend admin sync | Created by Terraform (`ory_project_api_key.tf`); value in state only. |

> ⚠️ **Rotate the workspace key.** The current `ory_wak_` (name `terraform-auth-stack`, expires
> 2026-11-17) was generated during an assistant session, so treat it as exposed: generate a fresh
> workspace API key in the Ory Console → workspace `auth-stack` → Settings → API keys, update the
> `ORY_WORKSPACE_API_KEY` GitHub secret / HCP var, and delete the old one.

Nothing sensitive is committed: `.gitignore` excludes `*.tfstate`, `.terraform/`, and `tfplan`.

## Run it

```bash
cd infra/ory
export ORY_WORKSPACE_API_KEY='ory_wak_…'   # from the GitHub secret / your console
export ORY_WORKSPACE_ID='1a710b61-9aaa-473c-aab5-77b5a5f645ad'
terraform init
terraform plan  -var-file=environments/dev.tfvars
terraform apply -var-file=environments/dev.tfvars
```

## State — currently local, migrate to HCP Terraform

The bootstrap apply used **local state** (HCP Terraform wasn't authenticated in the provisioning
session). To move to HCP (org `jamescrowley321`, matching the Descope track's `identity-stack-dev`
workspace) **without recreating anything**:

1. Create/confirm an HCP workspace, e.g. `auth-stack-ory-dev`.
2. Add a `cloud {}` block to `versions.tf`:
   ```hcl
   cloud {
     organization = "jamescrowley321"
     workspaces { name = "auth-stack-ory-dev" }
   }
   ```
3. Set `ORY_WORKSPACE_API_KEY` (sensitive) + `ORY_WORKSPACE_ID` as workspace **env** variables in HCP.
4. `terraform init -migrate-state` — uploads existing local state to HCP (no resource churn).

## Convergence pattern (auth repos + beyond)

`modules/ory-oidc-app` is the reusable unit. Each consumer instantiates it with its own inputs and
its own HCP workspace/state:

- **identity-stack** — this root (`identity-stack-dev`; add `-stage`/`-prod` module blocks as needed).
- **py-identity-model** — its integration project can be brought under the same module (it already
  validates JWT access tokens from an Ory project via `TEST_DISCO_ADDRESS`).
- **identity-model** — same module for its conformance/integration project.
- **~/repos/gis** and other domains — reuse the module with their own project/workspace; auth-repo
  projects converge in the `auth-stack` Ory workspace, unrelated domains get their own workspace.

To share the module across separate git repos, promote `modules/ory-oidc-app` to a Terraform
registry module or a `git::`-sourced module; until then, vendor/symlink it per repo.

## Caveats

- **Create-only / no-import:** Ory identity schemas, API-key values, and secrets cannot be imported
  (drift-managed). Treat the schema as replace-not-import.
- **Workspaces are console-only:** `ory_workspace` cannot be created by Terraform — only imported.
  The `auth-stack` workspace was created in the Console; import it later if you want it in state.
- **Develop tier:** free, EU-homed, rate-limited, **no PII storage** and **no Organizations** — hence
  canonical-side tenancy. Move to a paid/Production workspace before storing real user PII or using
  Ory Organizations.
