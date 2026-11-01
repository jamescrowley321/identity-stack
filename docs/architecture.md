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

Three issuer formats are supported (all signed by the same project JWKS), so validation must accept all:

- `https://api.descope.com/{project_id}` — OIDC / ID tokens
- `https://api.descope.com/v1/apps/{project_id}` — OIDC inbound-app tokens
- `{project_id}` — the bare project id, used by SDK / session / access-key tokens (e.g. the access-key exchange `sessionJwt`)

Token validation is delegated to [`py-identity-model`](https://github.com/jamescrowley321/py-identity-model) (`>=3.8.5`); the RBAC/FGA authorization layer sits on top of the validated claims (see [security.md](security.md)).

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
  repositories/   # SQL against the canonical model (§19)
  services/       # canonical services, sync adapters, descope client, reconciliation, identity resolution
  models/         # canonical DB models (identity/: tenant, role, provider, ...)
backend/migrations/ # Alembic migrations
frontend/         # React + Vite SPA
infra/            # Terraform (Descope project config; infra/ory/ for Ory)
tyk/              # gateway config + init sidecar
```

The rules the backend code follows (configuration, outbound HTTP, logging, results and errors, authorization, data access) are in [conventions.md](conventions.md). The decisions behind them are in [adr/](adr/README.md).
