# Local Development

The `Makefile` is the canonical entry point for development and testing — it wraps the underlying `uv`/`npm`/`docker compose` commands. Run `make help` to list every target.

## Setup

```bash
make setup          # install backend + frontend dependencies
```

## Running the app

```bash
make dev-backend    # start the FastAPI dev server (http://localhost:8000)
make dev-frontend   # start the Vite dev server (http://localhost:3000, proxies /api -> :8000)
make dev-gateway    # start the full stack with the Tyk gateway profile (DEPLOYMENT_MODE=gateway)
```

The backend needs a reachable database (`DATABASE_URL`) and the Descope variables — see [environment-variables.md](environment-variables.md).

## Testing

Tests run against an **isolated** Postgres+Redis stack defined in `docker-compose.test.yml`. Its ports are deliberately shifted to **`15432`** (Postgres) and **`16379`** (Redis) so it never collides with a running dev stack.

```bash
make test-up             # bring up the test stack (idempotent; leave it running between runs)
make test-unit           # backend unit tests (auto-brings-up the test stack)
make test-integration    # backend integration tests
make test-frontend       # frontend unit tests (vitest)
make test-e2e            # end-to-end tests (needs frontend + backend running)
make test-all            # lint + unit + frontend + integration
make test-down           # tear the test stack down
```

Deployment-mode-specific integration suites manage their own Compose lifecycle:

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
| `--profile gateway` (`docker-compose.gateway.yml`) | adds the Tyk gateway; backend runs in `gateway` mode |
| `--profile full` | full topology |
| `--profile infra` | infrastructure dependencies only |

`make dev-gateway` wires up the gateway profile for you. See [deployment.md](deployment.md) for what each mode requires.
