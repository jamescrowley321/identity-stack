# Environment Variables

Defaults below are the values baked into the code (`backend/app/...`). The backend reads every variable through `app/knobs.py` ([conventions §1–§2](conventions.md#1-configuration-is-read-only-through-appknobs)), and `tests/unit/test_house_rules.py` fails if a variable it reads is missing from the backend table or documented with a different default. Copy `backend/.env.example` and `frontend/.env.example` as starting points.

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
| `CSP_POLICY` | `default-src 'self'` in production; a localhost-friendly policy otherwise | `Content-Security-Policy` header value (`middleware/security.py`). |
| `ORY_ISSUER_URL` | `""` | Ory issuer URL. Set ⇒ Ory is added as a configured token provider alongside Descope (`middleware/providers.py`). |
| `ORY_AUDIENCE` | — | Ory access-token audience to enforce. Required when `ORY_ISSUER_URL` is set and audience validation is on. |
| `ORY_REQUIRE_AUDIENCE` | `true` | Ory audience validation switch. Only `false`, `0` or `no` turn it off; any other value leaves it on (`knob_flag`). |
| `ORY_CLIENT_ID` | — | OAuth2 client id sent on Ory RP-initiated logout. Unset or empty ⇒ `ORY_AUDIENCE`. |
| `ORY_POST_LOGOUT_REDIRECT_URI` | — | Where Ory sends the browser after logout. Unset or empty ⇒ `FRONTEND_URL` + `/login`. Must be registered on the Ory client. |
| `JIT_ENABLED_PROVIDERS` | `ory` | Comma-separated providers whose first-login users are provisioned just in time by `GET /api/identity`. |
| `CANONICAL_RBAC_PROVIDERS` | `ory` | Comma-separated providers whose roles and permissions come from the canonical model rather than token claims. Descope is excluded by default. |

> **Never set `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST` or `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE`.** The OpenTelemetry httpx instrumentor reads them itself and would record outbound headers as span attributes. That includes the `Authorization: Bearer <project>:<management key>` header on every Descope management call. The backend's span hook overwrites only the `authorization` attribute ([conventions §9](conventions.md#9-no-secrets-in-logs)).

> `py-identity-model` (token validation, OIDC discovery and JWKS) reads `HTTP_TIMEOUT` itself, default `30` seconds. The backend never reads it, so it is not in the table above.

> The three internal-endpoint secrets (`DESCOPE_WEBHOOK_SECRET`, `DESCOPE_FLOW_SYNC_SECRET`, `INTERNAL_IDENTITY_KEY`) do **not** block startup — the app logs a warning and the corresponding endpoint refuses traffic until the secret is set (`main.py:_warn_missing_secrets`).

## Compose-level (required to run the stack, not read by the app)

| Variable | Required for | Notes |
|----------|--------------|-------|
| `POSTGRES_PASSWORD` | any `docker compose up` | Hard-guarded with `${POSTGRES_PASSWORD:?}` — Compose **aborts** if unset. |
| `TYK_GATEWAY_SECRET` | gateway profile | Gateway admin/API secret (see [`tyk/README.md`](../tyk/README.md)). |

## Frontend (Vite — inlined at build time)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_OIDC_AUTHORITY` | — | Generic OIDC issuer/authority URL. When set, points the SPA login at any standard-OIDC provider and takes precedence over `VITE_DESCOPE_*`. |
| `VITE_OIDC_CLIENT_ID` | — | OIDC client ID for the SPA. Overrides `VITE_DESCOPE_PROJECT_ID`. |
| `VITE_OIDC_SCOPE` | `openid profile email` | Requested OAuth scopes (e.g. add `offline_access` for refresh tokens). |
| `VITE_OIDC_REDIRECT_URI` | current window origin | OIDC redirect URI. |
| `VITE_DESCOPE_PROJECT_ID` | — (**required** unless `VITE_OIDC_*` set) | Descope project ID for the SPA login (backward-compat fallback). |
| `VITE_DESCOPE_BASE_URL` | `https://api.descope.com` | Optional; set for a custom auth domain. |
| `VITE_API_BASE_URL` | `""` | Empty ⇒ relative `/api/...` (standalone, proxied by nginx). Gateway mode sets this to the Tyk URL so the browser hits the gateway directly. Because Vite inlines it at build time, each mode needs its own image. |

The `VITE_OIDC_*` variables are provider-neutral: when set they target the login flow at any standard-OIDC provider (e.g. Ory) and override the `VITE_DESCOPE_*` fallback per-field. Leave them unset to keep using Descope. See [`frontend/.env.example`](../frontend/.env.example) for a worked example.
