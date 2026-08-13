# Security

This documents the application-level security controls. For vulnerability reporting see [`../SECURITY.md`](../SECURITY.md).

## Token validation

Bearer tokens are validated by [`py-identity-model`](https://github.com/jamescrowley321/py-identity-model) (`>=3.8.5`, a security-pinned floor — it pulls `cryptography>=50` with the PYSEC-2026-3552/3553/3554 fixes). Validation accepts both Descope issuer formats:

- `https://api.descope.com/{project_id}`
- `https://api.descope.com/v1/apps/{project_id}`

In `gateway` mode, auth is terminated at the Tyk edge and the backend trusts gateway-forwarded identity headers instead (`middleware/factory.py`).

## Authorization (RBAC + FGA)

Route handlers enforce authorization with dependency guards built over the validated claims:

- `require_role(...)`, `require_admin_role(...)` — role-based checks against the token's `tenants`/`dct` claims.
- `require_permission(...)` — permission-based checks.
- `require_fga(...)` — fine-grained (ReBAC) authorization via Descope FGA.

## Transport & request hardening

- **Security headers** — `SecurityHeadersMiddleware` sets response hardening headers; strictness keys off `ENVIRONMENT`.
- **Rate limiting** — [slowapi](https://github.com/laurentS/slowapi): `RATE_LIMIT_DEFAULT` (60/min) and `RATE_LIMIT_AUTH` (10/min for auth-sensitive routes); keyed by the authenticated `sub` or by client IP. `GET /api/health` is exempt.
- **Trusted proxies** — `TRUSTED_PROXY_HOSTS` controls which forwarded headers are honored.
- **Internal endpoints** — `/api/internal/*` are each gated by a shared secret; an unset secret makes the endpoint reject all traffic (fail-closed).

## Supply-chain / CI scanning

Security scanning runs in CI (`.github/workflows/`), consistent with the workspace's GitHub-native + FOSS tooling posture:

- `codeql.yml` — CodeQL static analysis
- `osv-scanner.yml` — OSV dependency scanning
- `dependency-review.yml` — PR dependency review
- `scorecard.yml` — OpenSSF Scorecard
- `security.yml` — additional security checks

Locally, `make security` runs `pip-audit` + `npm audit`.

## Notable dependency pins (decision record)

- **`fastapi>=0.115.0,<0.137`** — capped because slowapi 0.1.10's `SlowAPIMiddleware` route matcher only recognizes top-level routes under the newer FastAPI inclusion model, which breaks rate limiting. Revisit when slowapi ships a fix (`backend/pyproject.toml`).
- **`py-identity-model>=3.8.5,<4`** — security floor (see Token validation above).
