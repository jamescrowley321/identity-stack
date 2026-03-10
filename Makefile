.DEFAULT_GOAL := help

.PHONY: help setup lint test-unit test-integration test-all dev-backend dev-frontend dev

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

test-all: lint test-unit test-integration ## run all checks

dev-backend: ## start backend dev server
	cd backend && uvicorn app.main:app --reload

dev-frontend: ## start frontend dev server
	cd frontend && npm run dev
