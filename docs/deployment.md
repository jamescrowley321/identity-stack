# Deployment

identity-stack runs in one of two modes, chosen with `DEPLOYMENT_MODE`.

## Standalone

The backend validates bearer tokens itself (`TokenValidationMiddleware`), and the frontend reaches it on the same origin (nginx proxies `/api/`). This is the default.

```bash
docker compose up --build
```

`docker-compose.yml` sets `DEPLOYMENT_MODE=standalone` on the backend itself, so the shell value does not matter here.

Required:

| Secret | Why |
|--------|-----|
| `POSTGRES_PASSWORD` | Compose aborts without it. |
| `DESCOPE_PROJECT_ID`, `DESCOPE_MANAGEMENT_KEY` | Token validation + provider sync. The backend refuses to start without either (`main.py` lifespan). |
| `DESCOPE_WEBHOOK_SECRET`, `DESCOPE_FLOW_SYNC_SECRET`, `INTERNAL_IDENTITY_KEY` | Only if you use the inbound sync/webhook/identity endpoints. |

## Gateway

A **Tyk** gateway terminates auth at the edge and forwards identity to the backend, which runs in `gateway` mode and trusts the forwarded headers. The frontend is built to call the gateway directly (`VITE_API_BASE_URL`).

```bash
make dev-gateway
# or: docker compose -f docker-compose.yml -f docker-compose.gateway.yml --profile gateway up --build
```

`--profile gateway` on its own adds the Tyk containers but leaves the backend in `standalone` mode; the `docker-compose.gateway.yml` override is what sets `DEPLOYMENT_MODE=gateway` and points the frontend build at Tyk (`http://localhost:8080`).

Additionally required: **`TYK_GATEWAY_SECRET`**, plus a valid `DESCOPE_PROJECT_ID` (the `tyk-init` sidecar aborts without it). Gateway internals (routing, header forwarding, the init sidecar) are documented in [`tyk/README.md`](../tyk/README.md).

## Required-secrets matrix

| Secret | Standalone | Gateway |
|--------|:----------:|:-------:|
| `POSTGRES_PASSWORD` | ✅ | ✅ |
| `DESCOPE_PROJECT_ID` | ✅ | ✅ |
| `DESCOPE_MANAGEMENT_KEY` | ✅ | ✅ |
| `TYK_GATEWAY_SECRET` | — | ✅ |
| internal-sync secrets | if used | if used |

Full variable reference: [environment-variables.md](environment-variables.md).

## Descope project configuration (Terraform)

The Descope project itself (tenants, roles, permissions, FGA, applications) is managed as code under `infra/` with the [descope provider fork](https://github.com/jamescrowley321/terraform-provider-descope):

```bash
cd infra
export DESCOPE_MANAGEMENT_KEY=...        # never commit this
export TF_VAR_descope_project_id=...     # required, no default
terraform init
TF_WORKSPACE=identity-stack-dev terraform plan  -var-file=environments/dev.tfvars
TF_WORKSPACE=identity-stack-dev terraform apply -var-file=environments/dev.tfvars
```

`infra/main.tf` keeps state in HCP Terraform (a `cloud {}` block that selects workspaces by tag), so `terraform init` needs an HCP Terraform login, and `TF_WORKSPACE` picks the workspace. Per-environment inputs live under `infra/environments/` (`dev.tfvars`, `prod.tfvars`). The Ory project is a separate Terraform root under `infra/ory/`; see [`infra/ory/README.md`](../infra/ory/README.md). This provisions the project that the running app authenticates against; run it before (or alongside) bringing up the app.
