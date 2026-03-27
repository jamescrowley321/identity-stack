# Descope SaaS Starter Kit

A comprehensive reference project demonstrating Descope's identity platform features using vendor-agnostic libraries.

## Architecture

```
┌────────────────────────────────────┐
│  React Frontend (Vite + TS)        │
│  - Tailwind CSS v4 + shadcn/ui    │
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
| UI Framework | Tailwind CSS v4 + shadcn/ui (Radix) | Design system and components |
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

## API Endpoints

### Authentication
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/logout` | Revoke user sessions via Descope Management API |
| POST | `/api/validate-id-token` | Validate an ID token server-side |
| GET | `/api/me` | Return ClaimsPrincipal from py-identity-model |
| GET | `/api/claims` | Return raw access token claims |

### Tenant Management
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/tenants` | Create a new tenant (Descope Management API) |
| GET | `/api/tenants` | List tenants the current user belongs to (from JWT) |
| GET | `/api/tenants/current` | Get current tenant context (`dct` claim) |
| GET | `/api/tenants/{id}/resources` | List tenant-scoped resources |
| POST | `/api/tenants/{id}/resources` | Create a tenant-scoped resource |

### Role Management (RBAC)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/roles/me` | Get current user's roles and permissions in active tenant |
| POST | `/api/roles/assign` | Assign roles to a user (requires owner/admin) |
| POST | `/api/roles/remove` | Remove roles from a user (requires owner/admin) |

### User Profile & Attributes
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/profile` | Load current user's profile and custom attributes |
| PATCH | `/api/profile` | Update a custom attribute (`department`, `job_title`, `avatar_url`) |
| GET | `/api/tenants/current/settings` | Load current tenant's custom attributes |
| PATCH | `/api/tenants/current/settings` | Update tenant attributes (requires owner/admin) |

### Access Keys
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/keys` | Create an access key (returns secret once, requires owner/admin) |
| GET | `/api/keys` | List access keys for current tenant |
| GET | `/api/keys/{id}` | Load a single access key |
| POST | `/api/keys/{id}/deactivate` | Revoke an access key |
| POST | `/api/keys/{id}/activate` | Reactivate an access key |
| DELETE | `/api/keys/{id}` | Permanently delete an access key |

All access key operations require owner/admin role and verify the key belongs to the caller's tenant.

### Member Management
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/members` | List members in current tenant (requires owner/admin) |
| POST | `/api/members/invite` | Invite user by email with role assignment |
| POST | `/api/members/{id}/deactivate` | Deactivate a member |
| POST | `/api/members/{id}/activate` | Reactivate a member |
| DELETE | `/api/members/{id}` | Remove a member permanently |

Tenant isolation is enforced via the `dct` (Descope current tenant) JWT claim. Users can only access resources belonging to their active tenant.

### RBAC

Four roles are defined via Terraform (`infra/rbac.tf`):

| Role | Description | Key Permissions |
|------|-------------|-----------------|
| `owner` | Full access including billing | All permissions |
| `admin` | Full access except billing | All except `billing.manage` |
| `member` | Standard read/write access | Read/write documents, invite members |
| `viewer` | Read-only access | Read projects and documents |

Backend endpoints enforce authorization via `require_role()` and `require_permission()` dependency factories. Frontend uses `<RequireRole>` and `<RequirePermission>` components for conditional UI rendering.

### Security Headers

All API responses include security headers via `SecurityHeadersMiddleware`:

| Header | Value |
|--------|-------|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `X-XSS-Protection` | `0` (disabled in favor of CSP) |
| `Content-Security-Policy` | `default-src 'self'` (configurable via `CSP_POLICY`) |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` (production only) |

Set `ENVIRONMENT=production` to enable HSTS and strict CSP. Override CSP with `CSP_POLICY` env var.

### Rate Limiting

API endpoints are rate limited via [slowapi](https://github.com/laurentS/slowapi) to protect against abuse:

| Tier | Limit | Endpoints |
|------|-------|-----------|
| Auth-sensitive | 10/minute | `POST /auth/logout`, `POST /validate-id-token`, `POST /keys`, `POST /members/invite` |
| Default | 60/minute | All other API endpoints |
| Exempt | No limit | `GET /health` |

Rate limits are applied per-route per-key. Authenticated requests are keyed by user `sub` claim; unauthenticated requests are keyed by client IP.

When a limit is exceeded, the API returns `429 Too Many Requests` with a `Retry-After` header.

Configure limits via environment variables:

```bash
RATE_LIMIT_DEFAULT=60/minute   # Default limit for all endpoints
RATE_LIMIT_AUTH=10/minute      # Stricter limit for auth-sensitive endpoints
```

## Project Structure

```
descope-saas-starter/
├── frontend/          # React + Vite + TypeScript
│   ├── src/
│   │   ├── pages/     # Route pages
│   │   ├── components/# UI components
│   │   │   └── ui/   # shadcn/ui components
│   │   ├── hooks/     # Custom React hooks
│   │   └── api/       # Backend API client
│   └── ...
├── backend/           # FastAPI + py-identity-model
│   ├── app/
│   │   ├── middleware/ # Token validation, rate limiting, security headers
│   │   ├── dependencies/# Auth dependencies
│   │   ├── routers/   # API routes
│   │   ├── models/    # DB models
│   │   └── services/  # Business logic
│   └── ...
├── infra/             # Terraform (descope provider fork)
│   ├── main.tf
│   ├── project.tf
│   ├── access_key.tf
│   ├── tenants.tf     # Default tenant definitions
│   ├── rbac.tf        # Permissions and role definitions
│   └── environments/
└── docker-compose.yml
```
