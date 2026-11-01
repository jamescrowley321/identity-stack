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
make test-e2e            # end-to-end tests (needs frontend + backend running, Playwright and Descope credentials)
make test-all            # lint + unit + frontend + integration
make test-down           # tear the test stack down
```

The test targets load `backend/.env` when it exists. Deployment-mode-specific integration suites manage their own Compose lifecycle:

```bash
make test-integration-standalone   # standalone profile
make test-integration-gateway      # gateway profile (requires gateway env vars)
make test-gateway-proxy            # verify gateway proxying + header forwarding
```

## Lint & security

```bash
make lint       # ruff check + format (backend)
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

The backend's `REDIS_URL` points at the `redis` service, which only runs with `--profile infra`; without it the backend logs that Redis is unreachable and runs with cache invalidation disabled.

`make dev-gateway` wires up the gateway profile for you. See [deployment.md](deployment.md) for what each mode requires.
