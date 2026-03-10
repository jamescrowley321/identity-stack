# Descope SaaS Starter Kit

A comprehensive reference project demonstrating Descope's identity platform features using vendor-agnostic libraries.

## Architecture

```
┌────────────────────────────────────┐
│  React Frontend (Vite + TS)        │
│  - react-oidc-context (OIDC auth)  │
│  - Tenant-aware routing            │
│  - Role-based UI rendering         │
└───────────────┬────────────────────┘
                │ REST API
┌───────────────▼────────────────────┐
│  FastAPI Backend (Python)          │
│  - py-identity-model (token authN) │
│  - RBAC middleware (authZ)         │
│  - Multi-tenant data isolation     │
└───────────────┬────────────────────┘
                │
┌───────────────▼────────────────────┐
│  Terraform (IaC)                   │
│  - terraform-provider-descope      │
│    (jamescrowley321 fork)          │
│  - Provisions entire Descope       │
│    project config                  │
└────────────────────────────────────┘
```

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Frontend | React + Vite + TypeScript | SPA |
| Auth (Frontend) | react-oidc-context + oidc-client-ts | Vendor-agnostic OIDC |
| Backend | FastAPI | REST API |
| Auth (Backend) | py-identity-model | Vendor-agnostic token validation |
| IaC | Terraform + descope provider | Descope project configuration |

## Prerequisites

- Node.js 22+
- Python 3.12+
- Go 1.22+ (for building the Terraform provider)
- Terraform 1.5+
- A Descope account with a management key

## Getting Started

### 1. Clone and setup

```bash
git clone git@github.com:jamescrowley321/descope-saas-starter.git
cd descope-saas-starter
```

### 2. Build the Terraform provider (fork)

```bash
cd ~/repos/terraform-provider-descope
make dev  # installs binary + creates ~/.terraformrc with dev_overrides
```

### 3. Provision Descope project

```bash
cd infra
export DESCOPE_MANAGEMENT_KEY=your-management-key
terraform init
terraform apply -var-file=environments/dev.tfvars
```

Terraform provisions the full project config including an OIDC application and access key for integration tests. After applying, retrieve the test credentials:

```bash
terraform output integration_test_access_key_id         # → DESCOPE_CLIENT_ID
terraform output integration_test_access_key_cleartext   # → DESCOPE_CLIENT_SECRET
```

### 4. Run the backend

```bash
cd backend
cp .env.example .env
# Edit .env with your DESCOPE_PROJECT_ID
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

### 5. Run the frontend

```bash
cd frontend
cp .env.example .env
# Edit .env with your DESCOPE_PROJECT_ID
npm install
npm run dev
```

### Or use Docker Compose

```bash
# Create a .env in the project root with your Descope credentials:
#   DESCOPE_PROJECT_ID=your-project-id
#   DESCOPE_MANAGEMENT_KEY=your-management-key
docker compose up --build
```

The frontend is available at http://localhost:3000 and the backend at http://localhost:8000.

## Project Structure

```
descope-saas-starter/
├── frontend/          # React + Vite + TypeScript
│   ├── src/
│   │   ├── pages/     # Route pages
│   │   ├── components/# UI components
│   │   ├── hooks/     # Custom React hooks
│   │   └── api/       # Backend API client
│   └── ...
├── backend/           # FastAPI + py-identity-model
│   ├── app/
│   │   ├── middleware/ # Token validation
│   │   ├── dependencies/# Auth dependencies
│   │   ├── routers/   # API routes
│   │   ├── models/    # DB models
│   │   └── services/  # Business logic
│   └── ...
├── infra/             # Terraform (descope provider fork)
│   ├── main.tf
│   ├── project.tf
│   ├── access_key.tf
│   └── environments/
└── docker-compose.yml
```
