# Environment Variables

Defaults below are the values baked into the code (`backend/app/...`). Copy `backend/.env.example` and `frontend/.env.example` as starting points.

## Backend

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | — (**required**) | SQLAlchemy database URL. The app raises at startup if unset or unreachable (`models/database.py`). |
| `DEPLOYMENT_MODE` | `standalone` | `standalone` (backend validates tokens) or `gateway` (Tyk terminates auth at the edge). Selects the middleware stack (`middleware/factory.py`). |
| `DESCOPE_PROJECT_ID` | `""` | Descope project ID; required for token validation and management calls. |
| `DESCOPE_MANAGEMENT_KEY` | `""` | Descope management key; required for provider sync and management operations. |
| `DESCOPE_BASE_URL` | `https://api.descope.com` | Descope API base URL (override for custom domains/testing). |
| `DESCOPE_WEBHOOK_SECRET` | `""` | Shared secret guarding `POST /api/internal/webhooks/descope`. Unset ⇒ the endpoint rejects all requests. |
| `DESCOPE_FLOW_SYNC_SECRET` | `""` | Shared secret guarding `POST /api/internal/users/sync`. Unset ⇒ the endpoint rejects all requests. |
| `INTERNAL_IDENTITY_KEY` | `""` | Shared secret guarding `GET /api/internal/identity`. Unset ⇒ the endpoint rejects all requests. |
| `IDENTITY_CACHE_TTL` | `300` | Identity-resolution cache TTL, seconds (`services/identity_resolution.py`). |
| `RATE_LIMIT_DEFAULT` | `60/minute` | Default per-route rate limit. |
| `RATE_LIMIT_AUTH` | `10/minute` | Rate limit for auth-sensitive routes. |
| `TRUSTED_PROXY_HOSTS` | `127.0.0.1` | Comma-separated proxy hosts trusted for forwarded headers. |
| `FRONTEND_URL` | `http://localhost:3000` | CORS allowed origin. |
| `REDIS_URL` | — | Redis URL for cross-instance cache invalidation. Unset ⇒ cache invalidation disabled (single-instance only). |
| `LOG_LEVEL` | `INFO` | Log level. |
| `ENVIRONMENT` | `development` | Environment name; influences security-header strictness. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `""` | OTLP endpoint; empty disables telemetry export. |
| `OTEL_SERVICE_NAME` | `identity-stack` | OpenTelemetry service name. |

> The three internal-endpoint secrets (`DESCOPE_WEBHOOK_SECRET`, `DESCOPE_FLOW_SYNC_SECRET`, `INTERNAL_IDENTITY_KEY`) do **not** block startup — the app logs a warning and the corresponding endpoint refuses traffic until the secret is set (`main.py:_warn_missing_secrets`).

## Compose-level (required to run the stack, not read by the app)

| Variable | Required for | Notes |
|----------|--------------|-------|
| `POSTGRES_PASSWORD` | any `docker compose up` | Hard-guarded with `${POSTGRES_PASSWORD:?}` — Compose **aborts** if unset. |
| `TYK_GATEWAY_SECRET` | gateway profile | Gateway admin/API secret (see [`tyk/README.md`](../tyk/README.md)). |

## Frontend (Vite — inlined at build time)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_DESCOPE_PROJECT_ID` | — (**required**) | Descope project ID for the SPA login. |
| `VITE_DESCOPE_BASE_URL` | Descope default | Optional; set for a custom auth domain. |
| `VITE_API_BASE_URL` | `""` | Empty ⇒ relative `/api/...` (standalone, proxied by nginx). Gateway mode sets this to the Tyk URL so the browser hits the gateway directly. Because Vite inlines it at build time, each mode needs its own image. |
