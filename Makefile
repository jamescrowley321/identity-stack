.DEFAULT_GOAL := help

.PHONY: help setup lint test-unit test-frontend test-integration test-e2e test-all security dev-backend dev-frontend dev

help: ## show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup: ## install all dependencies
	cd backend && python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
	cd frontend && npm install

lint: ## run linters
	cd backend && ruff check . && ruff format --check .

test-unit: ## run backend unit tests
	cd backend && python -m pytest tests/unit/ -v

test-integration: ## run backend integration tests (requires env vars)
	cd backend && python -m pytest tests/integration/ -v

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
