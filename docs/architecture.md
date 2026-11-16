# Architecture

identity-stack is an identity platform built around a **canonical identity store** (PostgreSQL) that the application owns, kept in sync with an external identity provider (Descope). Ory can be configured as a second token provider (`ORY_ISSUER_URL`). The application is provider-agnostic at its boundaries — token validation and the data model are written against OIDC/OAuth2 concepts, not a specific vendor SDK.

## Components

| Component | Stack | Role |
|-----------|-------|------|
| Backend | FastAPI (Python 3.12+) | REST API, token validation, canonical store, provider sync |
| Frontend | React 19 + Vite + TypeScript | SPA; OIDC login via `react-oidc-context` |
| Datastore | PostgreSQL + SQLModel/SQLAlchemy 2 + Alembic | Canonical identity store and migrations |
| Gateway (optional) | Tyk | Edge auth/routing in gateway deployment mode — see [`tyk/README.md`](../tyk/README.md) |
| Infra | Terraform + [descope provider fork](https://github.com/jamescrowley321/terraform-provider-descope) | Descope project configuration as code (`infra/`) |

The backend serves interactive API docs at **`/docs`** (Scalar) and **`/redoc`** (ReDoc); the OpenAPI schema is at `/openapi.json`.

## Deployment modes

The backend selects its middleware stack from the `DEPLOYMENT_MODE` environment variable (`backend/app/middleware/factory.py`):

- **`standalone`** (default) — the backend validates bearer tokens itself via `TokenValidationMiddleware`. The frontend calls the backend directly (nginx proxies `/api/` on the same origin).
- **`gateway`** — a Tyk gateway sits in front and terminates auth at the edge; the backend trusts gateway-forwarded identity headers. See [deployment.md](deployment.md) and [`tyk/README.md`](../tyk/README.md).

## Token model (Descope)

Descope JWTs use two custom claims the middleware understands:

- **`dct`** — the *current* tenant for the session.
- **`tenants`** — all tenant memberships, each with its roles and permissions.

Other providers' tokens carry neither. Each provider is one registration in `backend/app/middleware/providers.py`, with its own claims extractor if its tokens name a tenant. A provider listed in `CANONICAL_RBAC_PROVIDERS` (Ory by default) is authorized from the canonical store instead: the request's tenant is the caller's sole membership, or the one the `X-Tenant-ID` header selects among several ([conventions §18](conventions.md#18-one-resolver-decides-the-tenant-and-the-grants)).

Three issuer formats are supported (all signed by the same project JWKS), so validation must accept all:

- `https://api.descope.com/{project_id}` — OIDC / ID tokens
- `https://api.descope.com/v1/apps/{project_id}` — OIDC inbound-app tokens
- `{project_id}` — the bare project id, used by SDK / session / access-key tokens (e.g. the access-key exchange `sessionJwt`)

Token validation is delegated to [`py-identity-model`](https://github.com/jamescrowley321/py-identity-model) (`>=4.1.1`); the RBAC/FGA authorization layer sits on top of the validated claims (see [security.md](security.md)).

## Keycloak token model

A Keycloak realm token is for one organization and one user type
([ADR-0010](adr/0010-keycloak-token-model.md)). The backend reads five claims and nothing else
(`backend/app/middleware/keycloak.py`):

| Concept | In Keycloak | In the token | Read where |
|---|---|---|---|
| User type | A confidential "type client", such as `member` or `tenant-admin` | `aud`, exactly one value | Authentication: anything else is 401 |
| App | A public client, one per type | `azp`, the app client configured for that `aud` | Authentication: anything else is 401 |
| Tenant | An Organization | `organization`, exactly one alias | `get_tenant_id`: anything else is 403 |
| Permission | A client role of the type client, named `<domain>:<action>` | `resource_access[aud].roles`, a list of strings | `require_permission`. Missing or of the wrong type: the token names no tenant, 403 |
| Role | Not used | Nothing | `require_role` always answers 403 |

The tenant is the organization alias, an opaque string. It is not a Descope tenant id and not a
canonical tenant UUID, so every route that sends its tenant to Descope, parses it as a UUID or
gates by role refuses a Keycloak principal with 403. `GET /api/access-checks/documents-read` and
`GET /api/access-checks/settings-manage` answer one: each needs the permission in its name and
returns the tenant the resolver decided. `realm_access`, and Descope's `dct` and `tenants` when
they appear on a Keycloak token, are never read.

Keycloak assigns client roles to the user, not to the user in an organization, so every type is
declared one of two ways:

- **Portable** (`KEYCLOAK_PORTABLE_TYPES`). The token grants only the roles listed for the type
  in `KEYCLOAK_PORTABLE_PERMISSIONS`, which may hold only `<domain>:read` and `<domain>:list`
  permissions. Those hold in every organization the user belongs to.
- **Organization-bound** (`KEYCLOAK_ORGANIZATION_BOUND_TYPES`). The token is accepted only when
  the user belongs to exactly one organization and it is the token's. The lookup that answers
  this is not wired yet, so every token of such a type is refused with 403. A lookup that fails,
  or does not answer within `MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS`, is 502 and no grant.

Two limits follow. A portable permission holds in every organization the user belongs to. And
nothing ends access when a user leaves an organization before the token expires, which is 300
seconds with the realm's defaults: Keycloak issues no new token for that organization, but the
backend does not ask the directory about a portable token.

The backend refuses to start with a half-configured Keycloak provider: see the `KEYCLOAK_*`
rows of [environment-variables.md](environment-variables.md).

## Sync flows

The canonical store is the source of truth for the application; Descope is kept consistent with it through two flows:

1. **Outbound (API write → canonical store → provider).** Writes through the API persist to PostgreSQL, and the canonical services (`backend/app/services/user.py`, `role.py`, `permission.py`, `tenant.py`) push them to Descope through a sync adapter (`backend/app/services/adapters/descope.py`, wired in `backend/app/dependencies/identity.py`). A failed sync is logged and the write still succeeds; reconciliation catches the drift.
2. **Inbound (provider event → canonical store).** Descope flow/webhook events reach the internal endpoints and update the canonical store (`backend/app/routers/internal.py`, `backend/app/services/inbound_sync.py`), and a reconciliation pass repairs drift (`backend/app/routers/reconciliation.py`, `backend/app/services/reconciliation.py`):
   - `POST /api/internal/users/sync` — flow-driven user sync (guarded by `DESCOPE_FLOW_SYNC_SECRET`, sent as `X-Flow-Secret`).
   - `POST /api/internal/webhooks/descope` — Descope webhooks (guarded by `DESCOPE_WEBHOOK_SECRET`, checked against `X-Descope-Webhook-S256`).
   - `GET /api/internal/identity` — identity resolution (guarded by `INTERNAL_IDENTITY_KEY`, sent as `X-Identity-Key`; results cached for `IDENTITY_CACHE_TTL` seconds).
   - `POST /api/internal/reconciliation/run` — a full reconciliation between Postgres and Descope (guarded by `DESCOPE_FLOW_SYNC_SECRET`).

These secrets are **not** required for the app to boot, but each guarded endpoint rejects all requests until its secret is set (`backend/app/main.py:_warn_missing_secrets`).

## Directory map

```
backend/app/
  knobs.py        # the only reader of the environment (conventions §1)
  outbound.py     # the only importer of httpx: outbound_client + Upstream* errors (§4)
  logbook.py      # logbook(__name__): the only source of loggers (§7)
  middleware/     # token validation, claims, rate limiting, security headers, factory
  routers/        # API routes (auth, tenants, roles, fga, internal, ...)
  dependencies/   # auth, tenant, RBAC and FGA guards; service wiring
  errors/         # IdentityError kinds + RFC 9457 problem details
  domain/         # the pure core, one package per bounded context (§22-§27, ADR-0008)
  repositories/   # SQL against the canonical model (§19)
  services/       # canonical services, sync adapters, descope client, reconciliation, identity resolution
  models/         # canonical DB models (identity/: tenant, role, provider, ...)
backend/migrations/ # Alembic migrations
frontend/         # React + Vite SPA
infra/            # Terraform (Descope project config; infra/ory/ for Ory)
tyk/              # gateway config + init sidecar
```

The rules the backend code follows (configuration, outbound HTTP, logging, results and errors, authorization, data access, layers and the domain) are in [conventions.md](conventions.md). The decisions behind them are in [adr/](adr/README.md).
