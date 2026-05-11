.DEFAULT_GOAL := help

.PHONY: help setup lint test-up test-down test-unit test-frontend test-integration test-e2e test-all security dev-backend dev-frontend dev test-gateway-proxy dev-gateway test-integration-standalone test-integration-gateway seed

COMPOSE_TEST := docker compose -f docker-compose.test.yml
TEST_DATABASE_URL := postgresql+asyncpg://identity_test:identity_test@localhost:15432/identity_test
TEST_REDIS_URL := redis://localhost:16379/0

help: ## show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup: ## install all dependencies
	cd backend && python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
	cd frontend && npm install

lint: ## run linters
	cd backend && ruff check . && ruff format --check .

test-up: ## bring up postgres+redis for tests (idempotent; leave running between test runs)
	$(COMPOSE_TEST) up -d --wait

test-down: ## tear down the test stack
	$(COMPOSE_TEST) down -v

test-unit: test-up ## run backend unit tests (auto-brings-up test stack)
	set -a; [ -f backend/.env ] && . backend/.env; set +a; \
	cd backend && TEST_DATABASE_URL='$(TEST_DATABASE_URL)' TEST_REDIS_URL='$(TEST_REDIS_URL)' \
	python -m pytest tests/unit/ -v

test-integration: test-up ## run backend integration tests (auto-brings-up test stack)
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
