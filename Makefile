.DEFAULT_GOAL := help

.PHONY: help setup lint test-up test-down test-unit test-frontend test-integration test-e2e test-all security dev-backend dev-frontend dev test-gateway-proxy dev-gateway test-integration-standalone test-integration-gateway seed zitadel-up zitadel-reset zitadel-admin-pat zitadel-terraform-check zitadel-seed zitadel-frontend keycloak-seed

COMPOSE_TEST := docker compose -f docker-compose.test.yml
TEST_DATABASE_URL := postgresql+asyncpg://identity_test:identity_test@localhost:15432/identity_test
TEST_REDIS_URL := redis://localhost:16379/0
TEST_OPENFGA_URL := http://localhost:18380

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
	$(COMPOSE_TEST) up -d --wait

.PHONY: test-up-keycloak
test-up-keycloak: ## bring up the test stack plus keycloak-test (http://keycloak-test.localhost:18180)
	$(COMPOSE_TEST) --profile keycloak up -d --wait

.PHONY: test-up-openfga
test-up-openfga: ## bring up the test stack plus openfga-test (http://localhost:18380, Postgres storage)
	$(COMPOSE_TEST) --profile openfga up -d --wait

test-down: ## tear down the test stack, keycloak-test and openfga-test included
	$(COMPOSE_TEST) --profile keycloak --profile openfga down -v

.PHONY: openfga-terraform-check
openfga-terraform-check: ## apply infra/openfga to a running openfga-test (make test-up-openfga) in a scratch copy, check the second plan is empty and the model is model.json, then destroy
	infra/openfga/scripts/check-against-compose.sh

.PHONY: keycloak-terraform-check
keycloak-terraform-check: ## apply infra/keycloak to a fresh realm in a running keycloak-test (make test-up-keycloak), check plan is empty and the realm matches the import
	infra/keycloak/scripts/check-against-compose.sh

.PHONY: keycloak-terraform-integration
keycloak-terraform-integration: ## keycloak-terraform-check, then the backend Keycloak integration tests against the Terraform-built realm (needs the running test stack)
	set -a; [ -f backend/.env ] && . backend/.env; set +a; \
	TEST_DATABASE_URL='$(TEST_DATABASE_URL)' TEST_REDIS_URL='$(TEST_REDIS_URL)' \
	infra/keycloak/scripts/check-against-compose.sh --integration-tests

test-unit: test-up ## run backend unit tests (auto-brings-up test stack)
	set -a; [ -f backend/.env ] && . backend/.env; set +a; \
	cd backend && TEST_DATABASE_URL='$(TEST_DATABASE_URL)' TEST_REDIS_URL='$(TEST_REDIS_URL)' \
	python -m pytest tests/unit/ -v

test-integration: test-up-keycloak test-up-openfga ## run backend integration tests (auto-brings-up test stack, with keycloak-test and openfga-test)
	set -a; [ -f backend/.env ] && . backend/.env; set +a; \
	cd backend && TEST_DATABASE_URL='$(TEST_DATABASE_URL)' TEST_REDIS_URL='$(TEST_REDIS_URL)' TEST_OPENFGA_URL='$(TEST_OPENFGA_URL)' \
	python -m pytest tests/integration/ -v

test-frontend: ## run frontend unit tests
	cd frontend && npm test

test-e2e: ## run e2e tests (requires running frontend + backend)
	cd backend && python -m pytest tests/e2e/ -v

# The suite logs in to the realm the applied infra/keycloak root built: its
# base URL and name from KEYCLOAK_ISSUER_URL, the users' ids and passwords and
# the admin service account's secret from the outputs (tests/helpers/keycloak_target.py).
# PYTEST_ARGS passes extra pytest options (CI's screenshots and traces).
KEYCLOAK_TEST_TARGET_JQ := def setting(name): .[name] // error("backend_environment has no \(name): apply infra/keycloak again"); \
	(.backend_environment.value // error("no backend_environment output: apply infra/keycloak first")) as $$backend \
	| ($$backend | setting("KEYCLOAK_ISSUER_URL") \
		| capture("^(?<base>.*[^/])/realms/(?<realm>[^/]+)$$") \
		// error("KEYCLOAK_ISSUER_URL is not <base URL>/realms/<realm>")) as $$issuer \
	| { KEYCLOAK_TEST_BASE_URL: $$issuer.base, KEYCLOAK_TEST_REALM: $$issuer.realm, \
		KEYCLOAK_TEST_USER_IDS: (.user_ids.value // error("no user_ids output: apply infra/keycloak first") | tojson), \
		KEYCLOAK_TEST_USER_PASSWORDS: (.test_credentials.value.users // error("no test_credentials users output: apply infra/keycloak first") | tojson), \
		KEYCLOAK_TEST_ADMIN_CLIENT_SECRET: ($$backend | setting("KEYCLOAK_ADMIN_CLIENT_SECRET")) } \
	| to_entries[] | "export \(.key)=\(.value | @sh)"

.PHONY: test-e2e-keycloak
test-e2e-keycloak: ## run the shared UI and API e2e suite and the Keycloak suite against Keycloak (requires keycloak-test with infra/keycloak applied, the backend and the SPA built from its outputs on localhost:3000; see docs/keycloak.md)
	@outputs="$$(terraform -chdir=infra/keycloak output -json)" || { \
		echo "test-e2e-keycloak: no outputs; apply infra/keycloak first (docs/keycloak.md)." >&2; \
		exit 1; \
	}; \
	exports="$$(printf '%s' "$$outputs" | jq -er '$(KEYCLOAK_TEST_TARGET_JQ)')" || exit 1; \
	eval "$$exports"; \
	cd backend && E2E_PROVIDER=keycloak python -m pytest tests/e2e/ tests/e2e_keycloak/ -v $(PYTEST_ARGS)

# The suite reads the applied root's outputs (test users, client ids) from
# ZITADEL_TERRAFORM_OUTPUTS. PYTEST_ARGS passes extra pytest options (CI's
# screenshots and traces).
.PHONY: test-e2e-zitadel
test-e2e-zitadel: ## run the shared UI and API e2e suite and the Zitadel suite against Zitadel (requires the compose Zitadel with infra/zitadel applied and seeded, the backend and the Zitadel-built SPA on localhost:3000; see docs/zitadel.md)
	@outputs="$$(terraform -chdir=infra/zitadel output -json)" || { \
		echo "test-e2e-zitadel: no outputs; apply infra/zitadel first (docs/zitadel.md)." >&2; \
		exit 1; \
	}; \
	cd backend && E2E_PROVIDER=zitadel ZITADEL_TERRAFORM_OUTPUTS="$$outputs" python -m pytest tests/e2e/ tests/e2e_zitadel/ -v $(PYTEST_ARGS)

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

zitadel-terraform-check: ## apply infra/zitadel to a fresh local Zitadel (make zitadel-reset first), check plan is empty and read the instance back
	TF_VAR_zitadel_access_token="$$($(MAKE) -s zitadel-admin-pat)" infra/zitadel/scripts/check-against-compose.sh

.PHONY: zitadel-terraform-integration
zitadel-terraform-integration: ## zitadel-terraform-check, then the backend's live Zitadel suite (tests/integration_zitadel) against the applied root (make zitadel-reset and make test-up first)
	TEST_DATABASE_URL='$(TEST_DATABASE_URL)' TF_VAR_zitadel_access_token="$$($(MAKE) -s zitadel-admin-pat)" \
	infra/zitadel/scripts/check-against-compose.sh --integration-tests

# The fixture users linked are those holding a role in a canonical tenant; their
# role assignments, and the realm's permissions and roles, come in with the first
# reconciliation pass (docs/keycloak.md#reconciliation).
keycloak-seed: ## register the keycloak provider, link canonical tenants to the applied infra/keycloak root's organization aliases and the fixture users to its users, in DATABASE_URL (migrated database; idempotent)
	@environment="$$(terraform -chdir=infra/keycloak output -json backend_environment)" || exit 1; \
	issuer="$$(printf '%s' "$$environment" | jq -er .KEYCLOAK_ISSUER_URL)" || exit 1; \
	aliases="$$(terraform -chdir=infra/keycloak output -json organization_aliases)" || exit 1; \
	linked="$$(printf '%s' "$$aliases" | jq -e --slurpfile fixture infra/fixtures/test-identities.json \
		'with_entries(select($$fixture[0].tenants[.key].canonical))')" || exit 1; \
	ids="$$(terraform -chdir=infra/keycloak output -json user_ids)" || exit 1; \
	users="$$(printf '%s' "$$ids" | jq -e --slurpfile fixture infra/fixtures/test-identities.json \
		'$$fixture[0] as $$f | with_entries(.key as $$name | $$f.users[$$name] as $$user \
		| select($$user.roles | to_entries | any(.value != null and $$f.tenants[.key].canonical)) \
		| .value = {external_sub: .value, email: "\($$name)@\($$f.tenants[$$user.home].domain)", \
			given_name: $$user.first_name, family_name: $$user.last_name})')" || exit 1; \
	printf '%s' "$$linked" | (cd backend && KEYCLOAK_ISSUER_URL="$$issuer" python -m scripts.seed_keycloak --link-organizations) || exit 1; \
	printf '%s' "$$users" | (cd backend && KEYCLOAK_ISSUER_URL="$$issuer" python -m scripts.seed_keycloak --link-users)

zitadel-seed: ## link the applied infra/zitadel root to canonical tenants in DATABASE_URL (migrated database; idempotent)
	terraform -chdir=infra/zitadel output -json canonical_seed | (cd backend && python -m scripts.seed_zitadel)

# The SPA's VITE_OIDC_* values are the applied root's frontend_environment
# output. Only VITE_OIDC_* keys are exported; any other key in the output is an error.
# `docker compose up` without --build then starts this image.
zitadel-frontend: ## build the frontend image against the applied infra/zitadel root (its frontend_environment output)
	@env_json="$$(terraform -chdir=infra/zitadel output -json frontend_environment)" || { \
		echo "zitadel-frontend: no frontend_environment output; apply infra/zitadel first (docs/zitadel.md)." >&2; \
		exit 1; \
	}; \
	exports="$$(printf '%s' "$$env_json" | jq -er 'to_entries[] | if (.key | test("^VITE_OIDC_[A-Z_]+$$")) then "export \(.key)=\(.value | @sh)" else error("unexpected key \(.key)") end')" || exit 1; \
	eval "$$exports"; \
	$(COMPOSE_ZITADEL) build frontend

# The two environment maps every local provider root outputs, in .env form:
# backend_environment (sensitive: it holds the service accounts' secrets) for
# backend/.env, frontend_environment for frontend/.env. Values are single-quoted,
# so the output can also be sourced by a shell. A missing output or an entry
# that is not a variable name with a string value fails the target.
PROVIDER_ENV_JQ := def env(name): (.[name].value // error("no \(name) output: apply infra/\($$root) first")) \
	| to_entries[] \
	| if (.key | test("^[A-Z][A-Z0-9_]*$$")) and (.value | type == "string") \
	then "\(.key)=\(.value | @sh)" else error("\(name) has an unexpected entry \(.key)") end; \
	["\# backend/.env", env("backend_environment"), "", "\# frontend/.env", env("frontend_environment")] | .[]

.PHONY: keycloak-env zitadel-env
keycloak-env: ## print the applied infra/keycloak root's backend and frontend environment in .env form
zitadel-env: ## print the applied infra/zitadel root's backend and frontend environment in .env form
keycloak-env zitadel-env: %-env:
	@outputs="$$(terraform -chdir=infra/$* output -json)" || { \
		echo "$*-env: no outputs; apply infra/$* first." >&2; \
		exit 1; \
	}; \
	printf '%s' "$$outputs" | jq -r --arg root '$*' '$(PROVIDER_ENV_JQ)'
