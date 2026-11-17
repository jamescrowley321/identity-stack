# Environment Variables

Defaults below are the values baked into the code (`backend/app/...`). The backend reads every variable through `app/knobs.py` ([conventions §1–§2](conventions.md#1-configuration-is-read-only-through-appknobs)), and `tests/unit/test_house_rules.py` fails if a variable it reads is missing from the Backend table, or is documented there with a different literal default. Copy `backend/.env.example` and `frontend/.env.example` as starting points.

## Backend

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | — (**required**) | SQLAlchemy database URL. The app raises at startup if unset or unreachable (`models/database.py`). |
| `DEPLOYMENT_MODE` | `standalone` | `standalone` (backend validates tokens) or `gateway` (Tyk terminates auth at the edge). Selects the middleware stack (`middleware/factory.py`). |
| `DESCOPE_PROJECT_ID` | `""` | Descope project ID; required for token validation and management calls. The app refuses to start when it is unset (`main.py` lifespan). |
| `DESCOPE_MANAGEMENT_KEY` | `""` | Descope management key; required for provider sync and management operations. The app refuses to start when it is unset (`main.py` lifespan). |
| `DESCOPE_BASE_URL` | `https://api.descope.com` | Descope API base URL (override for custom domains/testing). |
| `DESCOPE_WEBHOOK_SECRET` | `""` | Shared secret guarding `POST /api/internal/webhooks/descope`. Unset ⇒ the endpoint rejects all requests. |
| `DESCOPE_FLOW_SYNC_SECRET` | `""` | Shared secret guarding `POST /api/internal/users/sync`. Unset ⇒ the endpoint rejects all requests. |
| `INTERNAL_IDENTITY_KEY` | `""` | Shared secret guarding `GET /api/internal/identity`. Unset ⇒ the endpoint rejects all requests. |
| `IDENTITY_CACHE_TTL` | `300` | Identity-resolution cache TTL, seconds (`services/identity_resolution.py`). |
| `RATE_LIMIT_DEFAULT` | `60/minute` | Default per-route rate limit. |
| `RATE_LIMIT_AUTH` | `10/minute` | Rate limit for auth-sensitive routes. |
| `TRUSTED_PROXY_HOSTS` | `127.0.0.1` | Comma-separated proxy hosts trusted for forwarded headers. |
| `FRONTEND_URL` | `http://localhost:3000` | CORS allowed origin. |
| `REDIS_URL` | — | Redis URL (`redis://` or `rediss://`) for cross-instance cache invalidation. Unset, another scheme or unreachable ⇒ cache invalidation disabled (single-instance only). |
| `LOG_LEVEL` | `INFO` | Log level. |
| `ENVIRONMENT` | `development` | `production` turns on HSTS and the strict default CSP, and switches logs to JSON (`middleware/security.py`, `logging_config.py`). |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `""` | OTLP endpoint; empty disables telemetry export. |
| `OTEL_SERVICE_NAME` | `identity-stack` | OpenTelemetry service name. |
| `CSP_POLICY` | `default-src 'self'` in production; a localhost-friendly policy otherwise | `Content-Security-Policy` header value (`middleware/security.py`). |
| `ORY_ISSUER_URL` | `""` | Ory issuer URL. Set ⇒ Ory is added as a configured token provider alongside Descope (`middleware/providers.py`). |
| `ORY_AUDIENCE` | — | Ory access-token audience to enforce. Required when `ORY_ISSUER_URL` is set and audience validation is on. |
| `ORY_REQUIRE_AUDIENCE` | `true` | Ory audience validation switch. Only `false`, `0` or `no` turn it off; any other value leaves it on (`knob_flag`). |
| `ORY_CLIENT_ID` | — | OAuth2 client id sent on Ory RP-initiated logout. Unset or empty ⇒ `ORY_AUDIENCE`. |
| `ORY_POST_LOGOUT_REDIRECT_URI` | — | Where Ory sends the browser after logout. Unset or empty ⇒ `FRONTEND_URL` + `/login`. Must be registered on the Ory client. |
| `KEYCLOAK_ISSUER_URL` | `""` | Keycloak realm issuer, `<KC_HOSTNAME>/realms/<realm>`, exactly as in the token's `iss`. Set ⇒ Keycloak is added as a configured token provider (`middleware/providers.py`). |
| `KEYCLOAK_AUDIENCES` | `""` | Comma-separated type client ids accepted as the access token's `aud` (case-sensitive). A token with no `aud`, or none of these, gets 401. Required when `KEYCLOAK_ISSUER_URL` is set: the backend refuses to start without it. |
| `KEYCLOAK_REQUIRE_HTTPS` | `true` | Require HTTPS for Keycloak discovery and JWKS, in token validation and in logout. Only `false`, `0` or `no` turn it off; any other value leaves it on (`knob_flag`). It may be off only for a local issuer: the backend refuses to start with it off unless the issuer host is `localhost`, a loopback address or a name ending in `.localhost`. The exemption is by the name as it is written, not by what the name resolves to: a `.localhost` name that DNS or a hosts file points at another machine is still trusted, so turn this off for local development and tests only. The backend logs a warning at startup whenever it is off. Plain HTTP on `localhost` or a loopback address is allowed either way. |
| `KEYCLOAK_APP_CLIENTS` | `""` | Comma-separated `<type client>=<app client>` pairs (case-sensitive): the `azp` a token must carry for each `aud`. A token with any other `azp`, or with more than one `aud`, gets 401. Required when `KEYCLOAK_ISSUER_URL` is set: the backend refuses to start unless every type client in `KEYCLOAK_AUDIENCES` has exactly one app client, and refuses an app client that is itself in `KEYCLOAK_AUDIENCES` and one app client named for two type clients (`azp` would no longer tell the types apart). Example: `member=member-app,tenant-admin=tenant-admin-app`. |
| `KEYCLOAK_PORTABLE_TYPES` | `""` | Comma-separated type clients whose tokens grant the same permissions in every organization the user belongs to, and so only the read-only permissions listed in `KEYCLOAK_PORTABLE_PERMISSIONS`. Every type client in `KEYCLOAK_AUDIENCES` must be here or in `KEYCLOAK_ORGANIZATION_BOUND_TYPES`, and not in both, or the backend refuses to start. |
| `KEYCLOAK_ORGANIZATION_BOUND_TYPES` | `""` | Comma-separated type clients whose tokens are accepted only for a user who belongs to exactly one organization, the token's, and whom that organization marks as holding the type (its `type:<type client>` attribute lists the user's id). The backend asks the Admin API through the `KEYCLOAK_DIRECTORY_CLIENT_*` service account on every such request ([conventions §18](conventions.md#18-one-resolver-decides-the-tenant-and-the-grants)). |
| `KEYCLOAK_PORTABLE_PERMISSIONS` | `""` | Comma-separated `<type client>=<permission>` pairs, one pair per permission: the client roles a portable type grants. Any other client role on its token is not granted. Every portable type needs at least one, each permission must be `<domain>:read` or `<domain>:list`, and a pair for a type that is not portable is refused; otherwise the backend refuses to start. Example: `member=documents:read`. |
| `KEYCLOAK_DIRECTORY_CLIENT_ID` | `""` | Client id of the service account the backend reads a user's organizations with (`GET /admin/realms/{realm}/organizations/members/{sub}/organizations`). Required when `KEYCLOAK_ISSUER_URL` is set: the backend refuses to start without it. The client holds only the `realm-management` roles `view-users` and `view-organizations` (both are needed, both are read-only) and the `kc-admin-api` client scope. Example: `identity-stack-directory`. |
| `KEYCLOAK_DIRECTORY_CLIENT_SECRET` | `""` | That client's secret. Required when `KEYCLOAK_ISSUER_URL` is set: the backend refuses to start without it. Never logged; the startup refusal names the variable, not the value. The compose realm's value is a local fixture. |
| `MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS` | `5` | How long, in seconds, a request waits for a provider's membership lookup (the check behind an organization-bound token). A lookup that has not answered by then is cancelled and the request gets 502 with no grant. Read once at startup; a value that is not a positive, finite number stops startup with `ProviderConfigurationError`. |
| `KEYCLOAK_POST_LOGOUT_REDIRECT_URI` | — | Where Keycloak sends the browser after logout. Unset or empty ⇒ `FRONTEND_URL` + `/login`. Must be registered on the app client. The logout client id is the token's `azp`. |
| `ZITADEL_ISSUER_URL` | `""` | Zitadel instance issuer, exactly as in the token's `iss` (local: `http://zitadel.localhost:8480`). Set ⇒ Zitadel is added as a configured token provider (`middleware/providers.py`). Authorization is canonical: list `zitadel` in `CANONICAL_RBAC_PROVIDERS`, or a Zitadel principal is granted nothing. |
| `ZITADEL_AUDIENCE` | `""` | The project id. A token whose `aud` does not contain it gets 401. Required when `ZITADEL_ISSUER_URL` is set: the backend refuses to start without it. |
| `ZITADEL_CLIENT_ID` | `""` | Client id of the SPA application (case-sensitive). A token whose `client_id` is anything else (another application in the project, a machine user) gets 401, and so does any token carrying `azp`, `at_hash`, `auth_time`, `sid` or `nonce` (an ID token). Also the client id sent on RP-initiated logout. Required when `ZITADEL_ISSUER_URL` is set: the backend refuses to start without it. |
| `ZITADEL_REQUIRE_HTTPS` | `true` | Require HTTPS for Zitadel: discovery and JWKS in token validation, the logout discovery fetch and the first-login UserInfo fetch. Only `false`, `0` or `no` turn it off; any other value leaves it on (`knob_flag`). While it is on, the backend refuses to start with an `http` issuer, loopback included. The local instance needs `false`; the backend logs a warning at startup whenever it is off. |
| `ZITADEL_POST_LOGOUT_REDIRECT_URI` | — | Where Zitadel sends the browser after logout. Unset or empty ⇒ `FRONTEND_URL` + `/login`. Must be registered on the SPA application. |
| `JIT_ENABLED_PROVIDERS` | `ory` | Comma-separated providers whose first-login users are provisioned just in time by `GET /api/identity`. A provider is provisioned for only when its row exists in the canonical `providers` table (`scripts/seed_ory.py`, `scripts/seed_keycloak.py`). Keycloak is opt-in (`ory,keycloak`): provisioning links a new identity to the canonical user with the same verified email, so adding it trusts the realm's `email_verified` across providers ([security.md](security.md)). Zitadel is opt-in too (`ory,zitadel`, row from `scripts/seed_zitadel.py`): a first-login user joins, as `member`, the one active tenant whose `external_org_id` is the token's organization (`urn:zitadel:iam:user:resourceowner:id`) and whose `external_org_provider_id` is the `zitadel` row, with `email`, `email_verified` and the names from UserInfo; a token with no organization, an organization no tenant is linked to, an email that is missing or unverified, or a UserInfo `sub` that differs from the token's provisions nothing. |
| `CANONICAL_RBAC_PROVIDERS` | `ory` | Comma-separated providers whose tenant, roles and permissions come from the canonical model rather than token claims. A caller in several tenants selects one with the `X-Tenant-ID` request header ([conventions §18](conventions.md#18-one-resolver-decides-the-tenant-and-the-grants)). Descope is excluded by default and **must not be listed**: its routes hand the tenant from its own token claims to Descope, so startup fails with `ProviderConfigurationError` when a provider that is Descope-managed is in this list. Keycloak **must not be listed** either: its tenant and permissions come from its token alone, and startup fails the same way when it is. A Zitadel deployment lists `zitadel` (`ory,zitadel`): Zitadel has no claims extractor, so unlisted it is granted nothing. |

> **Never set any `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*` variable** (`_CLIENT_REQUEST`, `_CLIENT_RESPONSE`, `_SERVER_REQUEST`, `_SERVER_RESPONSE`). The OpenTelemetry httpx and FastAPI instrumentors read them themselves and would record headers as span attributes. Outbound, that includes the `Authorization: Bearer <project>:<management key>` header on every Descope management call. Inbound, it includes callers' bearer tokens and the internal-endpoint secrets (`X-Identity-Key`, `X-Flow-Secret`, `X-Descope-Webhook-S256`). The backend's span hook overwrites only the `authorization` attribute ([conventions §9](conventions.md#9-no-secrets-in-logs)).

> `py-identity-model` (token validation, OIDC discovery and JWKS) reads no environment variables. `HTTP_TIMEOUT`, `JWKS_CACHE_TTL` and the other variables that releases before 5.0 read are ignored. The backend uses the library's defaults: a 30-second timeout on discovery and JWKS fetches, and discovery documents and key sets cached for one hour unless the provider's `Cache-Control: max-age` says otherwise.

> The three internal-endpoint secrets (`DESCOPE_WEBHOOK_SECRET`, `DESCOPE_FLOW_SYNC_SECRET`, `INTERNAL_IDENTITY_KEY`) do **not** block startup — the app logs a warning and the corresponding endpoint refuses traffic until the secret is set (`main.py:_warn_missing_secrets`).

## Compose-level (read by Compose, not by the app)

| Variable | Required for | Notes |
|----------|--------------|-------|
| `POSTGRES_PASSWORD` | any `docker compose up` | Hard-guarded with `${POSTGRES_PASSWORD:?}` — Compose **aborts** if unset. |
| `TYK_GATEWAY_SECRET` | gateway profile | Gateway admin/API secret (see [`tyk/README.md`](../tyk/README.md)). Compose aborts without it when the gateway profile is on. |
| `REDIS_PASSWORD` | optional | Password for the `redis` service (`infra` profile) and the backend's `REDIS_URL`. Defaults to `changeme`. |
| `TYK_REDIS_PASSWORD` | optional | Password for `tyk-redis` (gateway profile). Defaults to `changeme`. |

## Frontend (Vite — inlined at build time)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_OIDC_AUTHORITY` | — | Generic OIDC issuer/authority URL. When set, points the SPA login at any standard-OIDC provider and takes precedence over `VITE_DESCOPE_*`. |
| `VITE_OIDC_CLIENT_ID` | — | OIDC client ID for the SPA. Overrides `VITE_DESCOPE_PROJECT_ID`. |
| `VITE_OIDC_SCOPE` | `openid profile email` | Requested OAuth scopes (e.g. add `offline_access` for refresh tokens). |
| `VITE_OIDC_REDIRECT_URI` | current window origin | OIDC redirect URI. |
| `VITE_OIDC_TENANT_SCOPE_TEMPLATE` | — | The whole scope of a sign-in to one tenant, with `{tenant}` where the tenant id goes (e.g. `openid profile email organization:{tenant}`). Used only when `GET /api/identity` says the tenant comes from the token (`token_tenant`): choosing a tenant signs in again with this scope, and a token that does not name the chosen tenant is reported as a failed switch. Unset, such a tenant is shown with no switch. A value without exactly one `{tenant}` stops the SPA at startup. |
| `VITE_DESCOPE_PROJECT_ID` | — (**required** unless `VITE_OIDC_*` set) | Descope project ID for the SPA login (backward-compat fallback). |
| `VITE_DESCOPE_BASE_URL` | `https://api.descope.com` | Optional; set for a custom auth domain. |
| `VITE_API_BASE_URL` | `""` | Empty ⇒ relative `/api/...` (standalone, proxied by nginx). Gateway mode sets this to the Tyk URL so the browser hits the gateway directly. Because Vite inlines it at build time, each mode needs its own image. |

The `VITE_OIDC_*` variables are provider-neutral: when set they target the login flow at any standard-OIDC provider (e.g. Ory) and override the `VITE_DESCOPE_*` fallback per-field. Leave them unset to keep using Descope. See [`frontend/.env.example`](../frontend/.env.example) for a worked example. `docker compose up --build` passes a `VITE_OIDC_*` variable to the image build only when it is set in the shell or the root `.env`; one set there wins over `frontend/.env`, and one left unset there is read from `frontend/.env`.
