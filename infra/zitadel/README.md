# Zitadel instance in Terraform (`infra/zitadel`)

Terraform that declares everything inside the local Zitadel instance that identity-stack relies
on, and a seed that links it to canonical tenants. It uses the `zitadel/zitadel` provider
(`~> 3.8`) and `hashicorp/random` (`~> 3.6`).

It targets the compose Zitadel ([`docs/zitadel.md`](../../docs/zitadel.md)), started with
`make zitadel-up`. There is no hosted Zitadel. State is local (`terraform.tfstate` in this
directory) and git-ignored: the instance is disposable, and a remote run could not reach
`zitadel.localhost`. `.terraform.lock.hcl` is committed.

## What it creates

| Resource | What |
|---|---|
| `zitadel_default_login_policy.this` | The instance default login policy: `allow_register = false`, external identity providers off. The other values are a fresh instance's. |
| `zitadel_default_oidc_settings.this` | Access-token lifetime `var.access_token_lifetime` (15 minutes; Zitadel's default is 12 hours). The ID-token and refresh-token lifetimes are a fresh instance's. |
| `zitadel_org.platform` | Organization `identity-stack`, which owns the project and the machine user. |
| `zitadel_org.this` | Tenant organizations `Acme` and `Globex`, and `Unlinked`, which no canonical tenant names. |
| `zitadel_login_policy.platform`, `zitadel_login_policy.this` | Each organization's own login policy, the same as the default: `allow_register = false`. |
| `zitadel_project.this` | Project `identity-stack`: `project_role_check`, `has_project_check` and `project_role_assertion` off. |
| `zitadel_project_role.this` | Role definitions `owner`, `admin`, `member`, `viewer`. Nobody is granted one. |
| `zitadel_project_grant.this` | The project granted to `Acme` and `Globex`. |
| `zitadel_application_oidc.spa` | The SPA: user agent, authorization code with PKCE, auth method none, refresh-token grant, JWT access tokens, no roles in the access token or ID token, redirect URI `var.frontend_url`, post-logout redirect `<frontend_url>/login`. Development mode is on while `frontend_url` is plain http, which Zitadel requires for an http redirect URI. |
| `zitadel_application_oidc.other` | A second application in the same project, for negative tests: the same settings without the refresh-token grant. A human's access token lists every application of the project in `aud`, so a token issued to this one proves audience alone does not identify the SPA. |
| `zitadel_machine_user.tests` | Machine user `identity-stack-tests` in `identity-stack`: JWT access tokens, a client secret for the client credentials grant. |
| `zitadel_human_user.this`, `random_password.user` | One test user per organization (`acme-user`, `globex-user`, `unlinked-user`), email `<user>@<org>.test`, verified, with a generated password. |

The canonical store decides access. No user holds a Zitadel role grant and the project checks
no role, so no Zitadel role decides anything.

## Run it

```bash
make zitadel-up
cd infra/zitadel
export TF_VAR_zitadel_access_token="$(make -s -C ../.. zitadel-admin-pat)"
terraform init
terraform apply -var-file=environments/dev.tfvars
terraform output backend_environment
terraform output frontend_environment
terraform output -json test_user_passwords
```

The provider calls the instance as the bootstrap admin machine user (`terraform-admin`,
`IAM_OWNER`) with its personal access token. The token comes from the environment, never from
a tfvars file. It is minted per instance, so after `make zitadel-reset` export it again, delete
`terraform.tfstate` (it describes the old instance) and apply again.

Variables:

| Variable | Default | What |
|---|---|---|
| `zitadel_url` | `http://zitadel.localhost:8480` | The instance's base URL, which is its issuer. `http://` only for a loopback or `*.localhost` host (validated). |
| `zitadel_access_token` | none, sensitive | An `IAM_OWNER` personal access token: `TF_VAR_zitadel_access_token`. |
| `frontend_url` | `http://localhost:3000` | SPA origin: redirect URI, post-logout `<frontend_url>/login`. A bare origin; `http://` only for a loopback or `*.localhost` host (validated). |
| `access_token_lifetime` | `15m0s` | Instance access-token lifetime, written the way Zitadel answers it (`15m0s`, `1h0m0s`), so the next plan stays empty. |

## Outputs

| Output | What |
|---|---|
| `issuer_url` | The issuer, the `iss` of every token. |
| `project_id` | The project id, in `aud` of a human's access token. |
| `spa_client_id`, `other_client_id` | Client ids of the two applications. |
| `organization_ids` | `platform`, `acme`, `globex`, `unlinked`. |
| `test_user_ids`, `test_user_login_names` | Each test user's id (token `sub`) and login name. |
| `test_user_passwords` (sensitive) | Each test user's password. |
| `machine_user` | The machine user's id and client id. |
| `machine_user_client_secret` (sensitive) | Its client secret. |
| `frontend_environment` | Frontend `VITE_OIDC_AUTHORITY`, `VITE_OIDC_CLIENT_ID`, `VITE_OIDC_REDIRECT_URI`, `VITE_OIDC_SCOPE`. The scope asks for `offline_access` (a refresh token) and `urn:zitadel:iam:user:resourceowner`, which puts the user's organization in the access token. |
| `backend_environment` | Backend `ZITADEL_ISSUER_URL`, `ZITADEL_AUDIENCE` (the project id), `ZITADEL_CLIENT_ID` (the SPA) and `ZITADEL_REQUIRE_HTTPS`. |
| `canonical_seed` | What the seed reads. |

The provider marks client ids sensitive; the root outputs them as plain values, because a
client id is public.

## The canonical seed

After `apply`, against a migrated application database (`alembic upgrade head`):

```bash
DATABASE_URL=postgresql+asyncpg://... make zitadel-seed
```

It pipes `terraform output -json canonical_seed` to
[`backend/scripts/seed_zitadel.py`](../../backend/scripts/seed_zitadel.py), which writes:

- canonical tenants `acme` and `globex`, with `external_org_id` set to their organization ids;
- a `providers` row `zitadel` of type `oidc` on the issuer;
- the `acme` test user, linked (`idp_links`) on its Zitadel user id, with role `admin` in `acme`
  and `viewer` in `globex`, so the tenant switch has a user in two tenants. The roles are the
  global canonical roles of those names, created without permissions when missing.

A second run changes nothing. After `make zitadel-reset` and a new apply the ids are new, and a
run moves the tenants and the link to them. The `globex` test user is left for first login to
provision; `Unlinked` gets no tenant.

## Checking it

`make zitadel-terraform-check` ([`scripts/check-against-compose.sh`](scripts/check-against-compose.sh))
needs a fresh instance, so run `make zitadel-reset` first. It never starts, resets or stops a
container, and fails when the instance does not answer. It:

1. applies the root in a new temporary directory with its own state, copying only the root
   files git does not ignore;
2. runs `terraform plan` again and fails unless it is empty;
3. runs [`scripts/check_instance.py`](scripts/check_instance.py), which reads the instance back:
   the access-token lifetime; `allow_register` false in the default login policy and in each
   organization's own policy; the Login V2 register page of every organization shows no form,
   only "registration is disabled"; the project's checks, roles and grants; no user role grant
   in any organization; both applications' settings; the users' organizations, verified emails
   and the machine user's token type; and that no non-sensitive output contains the value of a
   sensitive output or the admin token.

The temporary directory holds generated secrets and is deleted when the check ends. It needs
`terraform`, `curl` and `python3`. It runs on Linux.

Two things the check observed about the instance, which it does not paper over:

- **Login V2 enforces `allow_register`; the API does not.** The register page refuses, but the
  Login V2 service user (`login-client`) can still create a user in a tenant organization
  through the user API. Its token stays on the bootstrap volume and must not leave it.
- **Login V2 caches login settings it has fetched.** A register page loaded before the apply can
  still show the form afterwards. A fresh instance has nothing cached, which is why the check
  needs one.
