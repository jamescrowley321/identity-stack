# Local Development

The `Makefile` is the canonical entry point for development and testing — it wraps the underlying `uv`/`npm`/`docker compose` commands. Run `make help` to list every target.

## Setup

```bash
make setup          # install backend + frontend dependencies (uv sync --locked, npm install)
```

## Running the app

```bash
make dev-backend    # start the FastAPI dev server (http://localhost:8000)
make dev-frontend   # start the Vite dev server (http://localhost:3000, proxies /api -> :8000)
make dev-gateway    # start the full stack with the Tyk gateway profile (DEPLOYMENT_MODE=gateway)
```

The backend needs a reachable database (`DATABASE_URL`) plus `DESCOPE_PROJECT_ID` and `DESCOPE_MANAGEMENT_KEY`, or it refuses to start — see [environment-variables.md](environment-variables.md). `make dev-backend` runs `uvicorn` in `backend/` and does not load `backend/.env`, so export the variables first (or run `uvicorn app.main:app --reload --env-file .env` yourself).

## Testing

Tests run against an **isolated** Postgres+Redis stack defined in `docker-compose.test.yml`. Its ports are deliberately shifted to **`15432`** (Postgres) and **`16379`** (Redis) so it never collides with a running dev stack.

```bash
make test-up             # bring up the test stack (idempotent; leave it running between runs)
make test-unit           # backend unit tests (auto-brings-up the test stack, so it needs Docker)
make test-integration    # backend integration tests (auto-brings-up the test stack)
make test-frontend       # frontend unit tests (vitest)
make test-e2e            # the shared end-to-end suite against Descope (needs frontend + backend running, Playwright and Descope credentials)
make test-e2e-keycloak   # the shared suite and the Keycloak suite against Keycloak, no credential (needs keycloak-test, the backend and the Keycloak-built SPA; see keycloak.md)
make test-e2e-zitadel    # the shared suite and the Zitadel suite against Zitadel, no credential (needs the applied and seeded local Zitadel, the backend and the Zitadel-built SPA; see zitadel.md)
make test-all            # lint + unit + frontend + integration
make test-down           # tear the test stack down
```

The test targets load `backend/.env` when it exists. Deployment-mode-specific integration suites manage their own Compose lifecycle:

```bash
make test-integration-standalone   # standalone profile
make test-integration-gateway      # gateway profile (requires gateway env vars)
make test-gateway-proxy            # verify gateway proxying + header forwarding
```

### End-to-end suites

`backend/tests/e2e/` is the shared UI (Playwright, Chromium) and API suite: login, session,
identity, tenant switch, RBAC, documents and logout. It runs against the provider
`E2E_PROVIDER` names: `descope` (the default, what `make test-e2e` and CI's `E2E Tests` job
run), `keycloak` or `zitadel`. Everything that differs between providers sits behind one
interface in `backend/tests/e2e/helpers/providers.py`: the member and admin principals' API
tokens, a browser signed in to the SPA as either, and the tenant they act in. Keycloak and
Zitadel sign in through the SPA and the provider's own forms with the test users of
`infra/fixtures/test-identities.json`; they need no credential beyond what the applied
Terraform root outputs.

A test that cannot apply to a provider is listed in one table,
`backend/tests/e2e/not_applicable.py`, with the reason: the run deselects it and lists it
with its reason at the end. Nothing is skipped by environment, and a Keycloak or Zitadel run
fails, never skips, when the instance, the backend or the SPA does not answer.

`make test-e2e-keycloak` and `make test-e2e-zitadel` run the shared suite with
`E2E_PROVIDER` set, then the provider's own suite (`backend/tests/e2e_keycloak/`,
`backend/tests/e2e_zitadel/`), in one pytest run. They do not start anything: bring up the
instance, apply its Terraform root and start the backend and the SPA first, as
[keycloak.md](keycloak.md#running-the-end-to-end-suite-locally) and [zitadel.md](zitadel.md#end-to-end) list. From a
clean checkout that is Docker, Terraform, Node and the backend's Python environment only.
CI runs both on every pull request (`E2E Tests (Keycloak)`, `E2E Tests (Zitadel)`).

The internal-endpoint tests compute their headers and HMACs from `INTERNAL_IDENTITY_KEY`,
`DESCOPE_WEBHOOK_SECRET` and `DESCOPE_FLOW_SYNC_SECRET`. Give the backend and the suite the
same throwaway values (CI generates them per run, e.g. `openssl rand -hex 32`). A Keycloak or
Zitadel run fails at the start when one of the three is unset; a Descope run skips those tests.

## Lint & security

```bash
make lint       # ruff check + format, import-linter contracts (backend)
make security   # pip-audit + npm audit
```

## Compose profiles

The base `docker-compose.yml` composes with profiles for the larger topologies:

| Invocation | Brings up |
|-----------|-----------|
| `docker compose up` | backend + frontend + Postgres (standalone) |
| `--profile gateway` | adds `tyk-init`, `tyk-gateway` and `tyk-redis`; the backend stays in `standalone` mode unless you also pass `-f docker-compose.gateway.yml` |
| `-f docker-compose.yml -f docker-compose.gateway.yml --profile gateway` | the gateway topology with the backend in `gateway` mode and the frontend built against Tyk (what `make dev-gateway` runs) |
| `--profile full` | the same services as `gateway` (a placeholder for future services) |
| `--profile infra` | adds Redis and the Aspire dashboard (OTLP traces on `localhost:4317`, UI on `localhost:18888`) |
| `--profile zitadel` | adds a local Zitadel issuer at `http://zitadel.localhost:8480`. Start it alone with `make zitadel-up`; see [zitadel.md](zitadel.md) |

The backend's `REDIS_URL` points at the `redis` service, which only runs with `--profile infra`; without it the backend logs that Redis is unreachable and runs with cache invalidation disabled.

`make dev-gateway` wires up the gateway profile for you. See [deployment.md](deployment.md) for what each mode requires.

## Local Keycloak

The `keycloak` profile runs Keycloak 26 with the `identity-stack` realm imported at start from `keycloak/realm/identity-stack-realm.json`. There is no step in the admin console.

```bash
docker compose --profile keycloak up --wait            # the default stack plus Keycloak
docker compose --profile keycloak up --wait keycloak   # Keycloak alone
```

Compose reads the whole file whichever services you start, so `.env` needs the values any other invocation needs (`POSTGRES_PASSWORD`, `TYK_GATEWAY_SECRET`, `DESCOPE_PROJECT_ID`; see [deployment.md](deployment.md)). Keycloak itself uses none of them.

| | |
|---|---|
| Issuer | `http://keycloak.localhost:8180/realms/identity-stack` |
| Discovery | `http://keycloak.localhost:8180/realms/identity-stack/.well-known/openid-configuration` |
| Admin console | `http://keycloak.localhost:8180/admin`, user `admin`, password `admin` |
| Realm contents, users and passwords | [`keycloak/fixtures/README.md`](../keycloak/fixtures/README.md) |

Everything in the realm file and the admin password are local fixtures. The container keeps no volume: recreating it re-imports the realm, and anything changed in the console is lost.

### How the hostname resolves

`KC_HOSTNAME` is the fixed URL `http://keycloak.localhost:8180`, so the issuer is the same string for every caller. Keycloak listens on 8180 inside the container too, which keeps the URL valid on both sides:

- **On the host and in the browser**, `*.localhost` names resolve to loopback (browsers do this themselves; so does systemd-resolved), and the port is published on `127.0.0.1:8180`. If a tool on your machine does not resolve the name, add `127.0.0.1 keycloak.localhost` to your hosts file.
- **In other compose containers**, `keycloak.localhost` is a network alias of the Keycloak service, so the name resolves to the container, not to the caller's own loopback.

The name is not `localhost` or `127.0.0.1`, so a client that allows plain HTTP only for loopback hosts has to be told to allow it for this issuer. The realm's SSL mode is `none`.

### Backend configuration

To have the backend accept this realm's tokens, set in `backend/.env`:

```bash
KEYCLOAK_ISSUER_URL=http://keycloak.localhost:8180/realms/identity-stack
KEYCLOAK_AUDIENCE=identity-stack-api   # the API client: the SPA's access tokens carry it in aud
KEYCLOAK_CLIENT_ID=identity-stack-spa  # the SPA client: the azp of its access tokens
KEYCLOAK_REQUIRE_HTTPS=false   # local only: allowed because the issuer host ends in .localhost
CANONICAL_RBAC_PROVIDERS=ory,keycloak   # roles and permissions from the canonical store
JIT_ENABLED_PROVIDERS=ory,keycloak      # opt-in: provision Keycloak users on first sign-in
```

To push canonical tenants, users and memberships to the realm instead of Descope, add the admin
client ([docs/keycloak.md](keycloak.md#admin-adapter)):

```bash
SYNC_PROVIDER=keycloak
KEYCLOAK_ADMIN_CLIENT_ID=identity-stack-admin
KEYCLOAK_ADMIN_CLIENT_SECRET=local-only-admin-client   # local fixture value
```

Register the provider row once, and link canonical tenants to the realm's organizations, so
that a first sign-in is provisioned: `cd backend && DATABASE_URL=... KEYCLOAK_ISSUER_URL=...
python -m scripts.seed_keycloak` registers the row, and `make keycloak-seed` does that and links
`acme` and `globex` (`tenants.external_org_id` set to each organization's alias) for a realm
built by `infra/keycloak`.

The backend refuses to start with only part of this set. See
[environment-variables.md](environment-variables.md) for each variable and
[architecture.md](architecture.md#keycloak-principals) for what the backend reads from a
Keycloak token.

The end-to-end suite that drives the SPA and backend against this realm from Chromium, and how to
run it locally, is in [keycloak.md](keycloak.md).

`docker-compose.test.yml` runs the same realm as `keycloak-test` on `http://keycloak-test.localhost:18180`, resolved the same way. It sits behind the `keycloak` profile there: `make test-up` leaves it out, `make test-up-keycloak` starts the test stack with it, and so does `make test-integration`.

