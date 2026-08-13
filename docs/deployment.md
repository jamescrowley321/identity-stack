# Deployment

identity-stack runs in one of two modes, chosen with `DEPLOYMENT_MODE`.

## Standalone

The backend validates bearer tokens itself (`TokenValidationMiddleware`), and the frontend reaches it on the same origin (nginx proxies `/api/`). This is the default.

```bash
DEPLOYMENT_MODE=standalone docker compose up --build
```

Required:

| Secret | Why |
|--------|-----|
| `POSTGRES_PASSWORD` | Compose aborts without it. |
| `DESCOPE_PROJECT_ID`, `DESCOPE_MANAGEMENT_KEY` | Token validation + provider sync. |
| `DESCOPE_WEBHOOK_SECRET`, `DESCOPE_FLOW_SYNC_SECRET`, `INTERNAL_IDENTITY_KEY` | Only if you use the inbound sync/webhook/identity endpoints. |

## Gateway

A **Tyk** gateway terminates auth at the edge and forwards identity to the backend, which runs in `gateway` mode and trusts the forwarded headers. The frontend is built to call the gateway directly (`VITE_API_BASE_URL`).

```bash
make dev-gateway
# or: docker compose --profile gateway -f docker-compose.yml -f docker-compose.gateway.yml up --build
```

Additionally required: **`TYK_GATEWAY_SECRET`**, plus a valid `DESCOPE_PROJECT_ID`. Gateway internals (routing, header forwarding, the init sidecar) are documented in [`tyk/README.md`](../tyk/README.md).

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
terraform init
terraform plan
terraform apply
```

Per-environment inputs live under `infra/environments/`. This provisions the project that the running app authenticates against; run it before (or alongside) bringing up the app.
