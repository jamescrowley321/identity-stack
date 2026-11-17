.DEFAULT_GOAL := help

.PHONY: help setup lint test-up test-down test-unit test-frontend test-integration test-e2e test-all security dev-backend dev-frontend dev test-gateway-proxy dev-gateway test-integration-standalone test-integration-gateway seed zitadel-up zitadel-reset zitadel-admin-pat

COMPOSE_TEST := docker compose -f docker-compose.test.yml
TEST_DATABASE_URL := postgresql+asyncpg://identity_test:identity_test@localhost:15432/identity_test
TEST_REDIS_URL := redis://localhost:16379/0
# Runs a command holding the shared compose lock (re-entrant): every target
# that starts, uses or stops the test stack goes through it.
COMPOSE_LOCKED := scripts/with-compose-lock.sh
# Under the held lock (this make runs inside the helper, which sets
# IDENTITY_STACK_COMPOSE_LOCK) every nested lock call is re-entrant, so two
# targets run in parallel would share the stack unexcluded: one could stop
# keycloak-test under the other. Run the targets one at a time there, -j or not.
ifneq ($(IDENTITY_STACK_COMPOSE_LOCK),)
.NOTPARALLEL:
endif

# The base compose file refuses to parse without these three variables. The
# Zitadel targets never start the services that read them, so placeholders keep
# the targets working from a checkout with no .env.
COMPOSE_ZITADEL := POSTGRES_PASSWORD="$${POSTGRES_PASSWORD:-unused}" \
	DESCOPE_PROJECT_ID="$${DESCOPE_PROJECT_ID:-unused}" \
	TYK_GATEWAY_SECRET="$${TYK_GATEWAY_SECRET:-unused}" \
	docker compose --profile zitadel
ZITADEL_SERVICES := zitadel-proxy zitadel-login zitadel-api zitadel-db

help: ## show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup: ## install all dependencies
	cd backend && uv sync --locked
	cd frontend && npm install

lint: ## run linters
	cd backend && ruff check . && ruff format --check . && lint-imports

test-up: ## bring up postgres+redis for tests (idempotent; leave running between test runs)
	$(COMPOSE_LOCKED) $(COMPOSE_TEST) up -d --wait

.PHONY: test-up-keycloak
test-up-keycloak: ## bring up the test stack plus keycloak-test (http://keycloak-test.localhost:18180)
	$(COMPOSE_LOCKED) $(COMPOSE_TEST) --profile keycloak up -d --wait

test-down: ## tear down the test stack, keycloak-test included
	$(COMPOSE_LOCKED) $(COMPOSE_TEST) --profile keycloak down -v

.PHONY: keycloak-terraform-check
keycloak-terraform-check: ## apply infra/keycloak to a fresh realm in keycloak-test, check plan is empty and the realm matches the import
	infra/keycloak/scripts/check-against-compose.sh

# The test targets hold the compose lock from bringing the stack up until the
# last test ends: the nested make runs the stack target and the suite under one
# lock, so no test-down or check cleanup can stop the stack in between.
.PHONY: locked-test-unit locked-test-integration
test-unit: ## run backend unit tests (auto-brings-up test stack)
	$(COMPOSE_LOCKED) $(MAKE) --no-print-directory locked-test-unit

locked-test-unit: test-up
	set -a; [ -f backend/.env ] && . backend/.env; set +a; \
	cd backend && TEST_DATABASE_URL='$(TEST_DATABASE_URL)' TEST_REDIS_URL='$(TEST_REDIS_URL)' \
	python -m pytest tests/unit/ -v

test-integration: ## run backend integration tests (auto-brings-up test stack, with keycloak-test)
	$(COMPOSE_LOCKED) $(MAKE) --no-print-directory locked-test-integration

locked-test-integration: test-up-keycloak
	set -a; [ -f backend/.env ] && . backend/.env; set +a; \
	cd backend && TEST_DATABASE_URL='$(TEST_DATABASE_URL)' TEST_REDIS_URL='$(TEST_REDIS_URL)' \
	python -m pytest tests/integration/ -v

test-frontend: ## run frontend unit tests
	cd frontend && npm test

test-e2e: ## run e2e tests (requires running frontend + backend)
	cd backend && python -m pytest tests/e2e/ -v

test-all: lint test-unit test-frontend test-integration ## run all checks

security: ## run security scans (pip-audit, npm audit)
	cd backend && pip-audit --desc --ignore-vuln CVE-2026-4539
	cd frontend && npm audit --omit=dev --audit-level=high

dev-backend: ## start backend dev server
	cd backend && uvicorn app.main:app --reload

dev-frontend: ## start frontend dev server
	cd frontend && npm run dev

test-gateway-proxy: ## verify gateway proxy and header forwarding (requires gateway profile)
	./scripts/test-gateway-proxy.sh

test-integration-standalone: ## run standalone profile integration tests (manages compose lifecycle)
	./scripts/test-integration-standalone.sh

test-integration-gateway: ## run gateway profile integration tests (manages compose lifecycle, requires env vars)
	COMPOSE_GATEWAY_OVERRIDE="-f docker-compose.yml -f docker-compose.gateway.yml" ./scripts/test-integration-gateway.sh

dev-gateway: ## start full stack with gateway profile (DEPLOYMENT_MODE=gateway via override)
	docker compose -f docker-compose.yml -f docker-compose.gateway.yml --profile gateway up --build

seed: ## seed all resources from Descope + create demo data (requires running stack)
	docker compose exec backend alembic upgrade head
	docker compose exec backend python -m scripts.seed_all

zitadel-up: ## start the local Zitadel issuer (http://zitadel.localhost:8480) and wait until healthy
	$(COMPOSE_ZITADEL) up -d --wait zitadel-proxy

zitadel-reset: ## delete the local Zitadel instance and its volumes, then start a fresh one
	$(COMPOSE_ZITADEL) down --volumes $(ZITADEL_SERVICES)
	$(MAKE) zitadel-up

zitadel-admin-pat: ## print the bootstrap admin machine user's personal access token (local-only)
	@pat="$$($(COMPOSE_ZITADEL) cp zitadel-api:/zitadel/bootstrap/admin.pat - 2>/dev/null | tar -xO 2>/dev/null)"; \
	if [ -z "$$pat" ]; then \
		echo "zitadel-admin-pat: no token found; the local Zitadel is not running. Run 'make zitadel-up' first." >&2; \
		exit 1; \
	fi; \
	printf '%s\n' "$$pat"
